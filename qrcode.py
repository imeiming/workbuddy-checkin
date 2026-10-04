"""
纯标准库 QR Code 生成器（SVG 输出）。

扫码登录需要把 authUrl 渲染成二维码。本模块实现 QR Code Model 2 编码：
  - 字节模式编码（UTF-8）
  - 版本自动选择（1..40，含 EC 级别 M）
  - Reed-Solomon 纠错
  - 矩阵掩码与格式/版本信息

仅依赖标准库，避免为一张二维码引入第三方包。
"""

from __future__ import annotations

# ---- GF(256) 伽罗瓦域（QR 标准本原多项式 0x11D）----

_EXP = [0] * 512
_LOG = [0] * 256


def _init_tables() -> None:
    value = 1
    for i in range(255):
        _EXP[i] = value
        _LOG[value] = i
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_init_tables()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_generator(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        poly = _poly_mul(poly, [1, _EXP[i]])
    return poly


def _poly_mul(a: list[int], b: list[int]) -> list[int]:
    result = [0] * (len(a) + len(b) - 1)
    for i, av in enumerate(a):
        if av == 0:
            continue
        for j, bv in enumerate(b):
            result[i + j] ^= _gf_mul(av, bv)
    return result


def _rs_encode(data: bytes, ec_count: int) -> list[int]:
    gen = _rs_generator(ec_count)
    remainder = [0] * ec_count
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        if factor:
            for i, coef in enumerate(gen[1:]):
                remainder[i] ^= _gf_mul(coef, factor)
    return remainder


# ---- 版本参数（EC 级别 M）----

# 版本: (总码字数, EC 每块码字, 组1块数, 组1数据码字, 组2块数, 组2数据码字)
VERSION_M = {
    1: (26, 10, 1, 16, 0, 0),
    2: (44, 16, 1, 28, 0, 0),
    3: (70, 26, 1, 44, 0, 0),
    4: (100, 18, 2, 32, 0, 0),
    5: (134, 24, 2, 43, 0, 0),
    6: (172, 16, 4, 27, 0, 0),
    7: (196, 18, 4, 31, 0, 0),
    8: (242, 22, 2, 38, 2, 39),
    9: (292, 22, 3, 36, 2, 37),
    10: (346, 26, 4, 43, 1, 44),
    11: (404, 30, 1, 50, 4, 51),
    12: (466, 22, 6, 36, 2, 37),
    13: (532, 22, 8, 37, 1, 38),
    14: (581, 24, 4, 40, 5, 41),
    15: (655, 24, 5, 41, 5, 42),
    16: (733, 28, 7, 45, 3, 46),
    17: (815, 28, 10, 46, 1, 47),
    18: (901, 26, 9, 43, 4, 44),
    19: (991, 26, 3, 44, 11, 45),
    20: (1085, 26, 3, 41, 13, 42),
}

_ALIGN_POS = {
    1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
    6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
    10: [6, 28, 50], 11: [6, 30, 54], 12: [6, 32, 58], 13: [6, 34, 62],
    14: [6, 26, 46, 66], 15: [6, 26, 48, 70], 16: [6, 26, 50, 74],
    17: [6, 30, 54, 78], 18: [6, 30, 56, 82], 19: [6, 30, 58, 86],
    20: [6, 34, 62, 90],
}


class QRError(Exception):
    """二维码容量不足等编码错误。"""


def _pick_version(data_len: int) -> int:
    for version in range(1, 21):
        _, ec, g1, d1, g2, d2 = VERSION_M[version]
        capacity = g1 * d1 + g2 * d2
        # 字节模式：4bit 模式指示 + 长度位(版本>=10 占16bit) + 数据 + 4bit 终止
        length_bits = 8 if version < 10 else 16
        needed = 4 + length_bits + data_len * 8
        if needed <= capacity * 8:
            return version
    raise QRError("内容过长，超出 QR Code Version 20-M 容量")


def _build_codewords(payload: bytes, version: int) -> list[int]:
    _, ec_count, g1, d1, g2, d2 = VERSION_M[version]
    capacity = g1 * d1 + g2 * d2
    length_bits = 8 if version < 10 else 16

    bits: list[int] = []

    def push(value: int, width: int) -> None:
        for i in range(width - 1, -1, -1):
            bits.append((value >> i) & 1)

    push(0b0100, 4)                      # 字节模式
    push(len(payload), length_bits)       # 字符计数
    for byte in payload:
        push(byte, 8)

    # 终止符
    for _ in range(min(4, capacity * 8 - len(bits))):
        bits.append(0)
    # 补齐到字节边界
    while len(bits) % 8:
        bits.append(0)

    codewords = [int("".join(str(b) for b in bits[i : i + 8]), 2) for i in range(0, len(bits), 8)]

    # 交替填充 0xEC / 0x11
    pad = [0xEC, 0x11]
    idx = 0
    while len(codewords) < capacity:
        codewords.append(pad[idx % 2])
        idx += 1

    # 分块 + RS 纠错
    data_blocks: list[list[int]] = []
    ec_blocks: list[list[int]] = []
    offset = 0
    for count, size in ((g1, d1), (g2, d2)):
        for _ in range(count):
            block = codewords[offset : offset + size]
            offset += size
            data_blocks.append(block)
            ec_blocks.append(_rs_encode(bytes(block), ec_count))

    # QR 标准的数据码字排列是「跨块交织」：先取各块的第 0 个字，
    # 再取各块的第 1 个字…… 之后依次是纠错码字的同样排列。
    final: list[int] = []
    max_data = max(len(b) for b in data_blocks)
    for i in range(max_data):
        for block in data_blocks:
            if i < len(block):
                final.append(block[i])
    for i in range(ec_count):
        for block in ec_blocks:
            final.append(block[i])
    return final


def _make_matrix(version: int, codewords: list[int]) -> list[list[int | None]]:
    size = version * 4 + 17
    matrix: list[list[int | None]] = [[None] * size for _ in range(size)]

    def place_finder(row: int, col: int) -> None:
        """绘制 7x7 定位图形及其外围 1 模块白边（分隔符）。"""
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = row + dr, col + dc
                if 0 <= r < size and 0 <= c < size:
                    # dr/dc ∈ [0,6] 为图形本体，[0,6] 边界为黑，中心 3x3 为黑
                    in_body = 0 <= dr <= 6 and 0 <= dc <= 6
                    black = (
                        in_body
                        and (
                            dr in (0, 6)
                            or dc in (0, 6)
                            or (2 <= dr <= 4 and 2 <= dc <= 4)
                        )
                    )
                    matrix[r][c] = 1 if black else 0

    place_finder(0, 0)
    place_finder(0, size - 7)
    place_finder(size - 7, 0)

    # 定时图形（第 6 行 / 第 6 列）。必须跳过定位图形与分隔符所占区域，
    # 否则会覆盖刚绘制的定位图形 —— 那会导致二维码无法被扫描器识别。
    for i in range(size):
        if matrix[6][i] is None:
            matrix[6][i] = 1 if i % 2 == 0 else 0
        if matrix[i][6] is None:
            matrix[i][6] = 1 if i % 2 == 0 else 0

    # 右上 / 左下的定位图形分隔符（7x7 之外的第 8 行 / 列）
    for i in range(8):
        if matrix[size - 8][i] is None:
            matrix[size - 8][i] = 0
        if matrix[i][size - 8] is None:
            matrix[i][size - 8] = 0

    # 校正图形（版本 2 起）
    for row in _ALIGN_POS[version]:
        for col in _ALIGN_POS[version]:
            # 与三个定位图形重叠的位置跳过
            if (row <= 8 and col <= 8) or (row <= 8 and col >= size - 9) or (row >= size - 9 and col <= 8):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    r, c = row + dr, col + dc
                    if 0 <= r < size and 0 <= c < size and matrix[r][c] is None:
                        edge = max(abs(dr), abs(dc))
                        matrix[r][c] = 1 if edge != 1 else 0

    # 固定黑模块（格式信息区的一部分）
    matrix[size - 8][8] = 1
    # 预留格式信息区。这些格子在绘制函数图形后仍是 None，必须显式标记为
    # 「不承载数据」，否则后续填数据会覆盖它们，二维码将无法被解码。
    reserved: set[tuple[int, int]] = set()
    for i in range(9):
        reserved.add((8, i))
        reserved.add((i, 8))
    for i in range(8):
        reserved.add((8, size - 1 - i))
        reserved.add((size - 1 - i, 8))
    # 固定黑模块位于格式信息区，一并保留
    reserved.add((size - 8, 8))

    # 填数据，同时记录哪些格子承载的是数据位（只有这些格子能被掩码）
    data_cells: set[tuple[int, int]] = set()
    bits: list[int] = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    upward = True
    bit_idx = 0
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if (row, c) in reserved or matrix[row][c] is not None:
                    continue
                matrix[row][c] = bits[bit_idx] if bit_idx < len(bits) else 0
                data_cells.add((row, c))
                bit_idx += 1
        upward = not upward
        col -= 2

    return matrix, data_cells  # type: ignore[return-value]


def _apply_mask(matrix: list[list[int | None]], mask: int, data_cells: set[tuple[int, int]]) -> None:
    """仅对数据区施加掩码。

    定位图形 / 定时图形 / 校正图形 / 格式信息区都属于函数图形，
    绝不能被掩码，否则二维码结构损坏、扫描器无法识别。
    """
    for row, col in data_cells:
        if matrix[row][col] is not None and _mask_bit(mask, row, col):
            matrix[row][col] ^= 1


def _mask_bit(mask: int, row: int, col: int) -> bool:
    if mask == 0:
        return (row + col) % 2 == 0
    if mask == 1:
        return row % 2 == 0
    if mask == 2:
        return col % 3 == 0
    if mask == 3:
        return (row + col) % 3 == 0
    if mask == 4:
        return (row // 2 + col // 3) % 2 == 0
    if mask == 5:
        return (row * col) % 2 + (row * col) % 3 == 0
    if mask == 6:
        return ((row * col) % 2 + (row * col) % 3) % 2 == 0
    return ((row + col) % 2 + (row * col) % 3) % 2 == 0


_FORMAT_BITS_M = {  # EC 级别 M 的格式信息（15 bit）
    0: 0b101010000010010,
    1: 0b101000100100101,
    2: 0b101111001111100,
    3: 0b101101101001011,
    4: 0b100010111111001,
    5: 0b100000011001110,
    6: 0b100111110010111,
    7: 0b100101010100000,
}


def _place_format(matrix: list[list[int | None]], mask: int) -> None:
    """写入 15 位格式信息（副本 1 + 副本 2）。

    注意：固定黑模块位于 (size-8, 8)，它属于格式信息区但恒为 1，
    必须在两处副本写入之后显式恢复，否则会被位 0 覆盖成 0。
    """
    size = len(matrix)
    bits = _FORMAT_BITS_M[mask]
    get = lambda i: (bits >> i) & 1

    # 副本 1：左上角
    for i in range(6):
        matrix[8][i] = get(i)
    matrix[8][7] = get(6)
    matrix[8][8] = get(7)
    matrix[7][8] = get(8)
    for i in range(9, 15):
        matrix[14 - i][8] = get(i)

    # 副本 2：右上 / 左下
    for i in range(8):
        matrix[size - 1 - i][8] = get(i)
    for i in range(8, 15):
        matrix[8][size - 15 + i] = get(i)

    # 固定黑模块：恒为 1，位于两副本的交界处
    matrix[size - 8][8] = 1


def _penalty(matrix: list[list[int | None]]) -> int:
    size = len(matrix)
    score = 0

    # 规则1：同色连续
    for lines in (matrix, list(zip(*matrix))):
        for line in lines:
            run = 1
            for i in range(1, size):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += 3 + (run - 5)

    # 规则2：2x2 同色块
    for r in range(size - 1):
        for c in range(size - 1):
            val = matrix[r][c]
            if val == matrix[r][c + 1] == matrix[r + 1][c] == matrix[r + 1][c + 1]:
                score += 3

    # 规则3：类定位图形
    pattern = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    rev = list(reversed(pattern))
    for lines in (matrix, list(zip(*matrix))):
        for line in lines:
            seq = list(line)
            for i in range(size - 10):
                window = seq[i : i + 11]
                if window == pattern or window == rev:
                    score += 40

    # 规则4：黑白比例
    dark = sum(1 for row in matrix for v in row if v == 1)
    ratio = dark * 100 / (size * size)
    score += int(abs(ratio - 50) // 5) * 10
    return score


def make_qr_matrix(text: str) -> list[list[int]]:
    """生成完整 QR 矩阵（1=黑）。"""
    payload = text.encode("utf-8")
    version = _pick_version(len(payload))
    codewords = _build_codewords(payload, version)
    base, data_cells = _make_matrix(version, codewords)

    best: list[list[int | None]] | None = None
    best_score: int | None = None
    for mask in range(8):
        # 每个掩码都从同一份基础矩阵重新复制，避免相互污染
        candidate = [row[:] for row in base]
        _apply_mask(candidate, mask, data_cells)
        _place_format(candidate, mask)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best_score = score
            best = candidate

    assert best is not None
    return [[0 if v is None else int(v) for v in row] for row in best]


def to_svg(text: str, *, border: int = 2, dark: str = "#000000", light: str = "#ffffff") -> str:
    """把文本渲染为 SVG 二维码字符串。"""
    matrix = make_qr_matrix(text)
    size = len(matrix) + border * 2
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240" '
        f'viewBox="0 0 {size} {size}" shape-rendering="crispEdges">',
        f'<rect width="{size}" height="{size}" fill="{light}"/>',
    ]
    for r, row in enumerate(matrix):
        for c, val in enumerate(row):
            if val:
                parts.append(
                    f'<rect x="{c + border}" y="{r + border}" width="1" height="1" fill="{dark}"/>'
                )
    parts.append("</svg>")
    return "".join(parts)
