"""Pure-Python PDF text stamp.

Adds an uncompressed Helvetica text overlay to every page by appending an
incremental update. No third-party PDF library. If the file is not a PDF we
can safely rewrite, the caller gets a one-page license PDF instead of the
original bytes.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass


class PdfError(Exception):
    pass


class Ref:
    def __init__(self, num: int, gen: int = 0):
        self.num = num
        self.gen = gen

    def __repr__(self) -> str:
        return f"Ref({self.num} {self.gen})"


class PdfStr:
    def __init__(self, data: bytes):
        self.data = data


class Name:
    def __init__(self, value: str):
        self.value = value


@dataclass
class Stream:
    info: dict
    data: bytes


@dataclass
class Offset:
    offset: int
    gen: int


@dataclass
class InStream:
    stm_num: int
    index: int


class Parser:
    def __init__(self, data: bytes, i: int = 0, resolver=None):
        self.data = data
        self.i = i
        self.resolver = resolver

    def skip(self) -> None:
        data = self.data
        n = len(data)
        i = self.i
        while i < n:
            c = data[i]
            if c in b" \t\r\n\x0c\x00":
                i += 1
                continue
            if c == 37:  # %
                i += 1
                while i < n and data[i] not in b"\r\n":
                    i += 1
                continue
            break
        self.i = i

    def peek(self, size: int = 1) -> bytes:
        self.skip()
        return self.data[self.i : self.i + size]

    def parse(self):
        self.skip()
        if self.i >= len(self.data):
            raise PdfError("unexpected end")
        c = self.data[self.i]
        if c == 47:  # /
            return self._name()
        if c == 40:  # (
            return self._literal()
        if c == 60:  # <
            if self.data[self.i : self.i + 2] == b"<<":
                return self._dict()
            return self._hex()
        if c == 91:  # [
            return self._array()
        if c in b"+-.0123456789":
            return self._number_or_ref()
        word = self._word()
        if word == b"true":
            return True
        if word == b"false":
            return False
        if word == b"null":
            return None
        raise PdfError(f"bad token {word[:20]!r}")

    def _word(self) -> bytes:
        self.skip()
        start = self.i
        data = self.data
        while self.i < len(data) and data[self.i] not in b" \t\r\n\x0c\x00()<>[]{}/%":
            self.i += 1
        if self.i == start:
            raise PdfError("empty word")
        return data[start : self.i]

    def _name(self) -> str:
        self.i += 1
        data = self.data
        out = bytearray()
        while self.i < len(data):
            c = data[self.i]
            if c in b" \t\r\n\x0c\x00()<>[]{}/%":
                break
            self.i += 1
            if c == 35 and self.i + 1 < len(data):
                hexpair = data[self.i : self.i + 2]
                try:
                    out.append(int(hexpair, 16))
                    self.i += 2
                    continue
                except ValueError:
                    pass
            out.append(c)
        return out.decode("latin-1")

    def _literal(self) -> PdfStr:
        self.i += 1
        data = self.data
        out = bytearray()
        while self.i < len(data):
            c = data[self.i]
            self.i += 1
            if c == 41:  # )
                return PdfStr(bytes(out))
            if c == 92:  # backslash
                if self.i >= len(data):
                    break
                e = data[self.i]
                self.i += 1
                mapping = {110: 10, 114: 13, 116: 9, 98: 8, 102: 12, 40: 40, 41: 41, 92: 92}
                if e in mapping:
                    out.append(mapping[e])
                elif e in b"\r\n":
                    if e == 13 and self.i < len(data) and data[self.i] == 10:
                        self.i += 1
                elif 48 <= e <= 55:
                    octal = bytes([e])
                    for _ in range(2):
                        if self.i < len(data) and 48 <= data[self.i] <= 55:
                            octal += bytes([data[self.i]])
                            self.i += 1
                        else:
                            break
                    out.append(int(octal, 8) & 255)
                else:
                    out.append(e)
            else:
                out.append(c)
        raise PdfError("unterminated string")

    def _hex(self) -> PdfStr:
        self.i += 1
        data = self.data
        hexes = bytearray()
        while self.i < len(data) and data[self.i] != 62:
            c = data[self.i]
            self.i += 1
            if c not in b" \t\r\n\x0c":
                hexes.append(c)
        if self.i >= len(data) or data[self.i] != 62:
            raise PdfError("unterminated hex")
        self.i += 1
        if len(hexes) % 2:
            hexes.append(48)
        try:
            return PdfStr(bytes.fromhex(hexes.decode("ascii")))
        except ValueError as exc:
            raise PdfError("bad hex string") from exc

    def _dict(self) -> dict:
        self.i += 2
        out = {}
        while True:
            self.skip()
            if self.data[self.i : self.i + 2] == b">>":
                self.i += 2
                break
            key = self.parse()
            if not isinstance(key, str):
                raise PdfError("dict key is not a name")
            out[key] = self.parse()
        self.skip()
        if self.data[self.i : self.i + 6] == b"stream":
            return self._stream(out)
        return out

    def _stream(self, info: dict) -> Stream:
        self.i += 6
        if self.i < len(self.data) and self.data[self.i] == 13:
            self.i += 1
            if self.i < len(self.data) and self.data[self.i] == 10:
                self.i += 1
        elif self.i < len(self.data) and self.data[self.i] == 10:
            self.i += 1
        length = info.get("Length")
        if isinstance(length, Ref):
            if self.resolver is None:
                raise PdfError("indirect length")
            length = self.resolver(length)
        if isinstance(length, float):
            length = int(length)
        if not isinstance(length, int) or length < 0:
            raise PdfError("bad stream length")
        blob = self.data[self.i : self.i + length]
        if len(blob) != length:
            raise PdfError("short stream")
        self.i += length
        self.skip()
        if self.data[self.i : self.i + 9] != b"endstream":
            raise PdfError("missing endstream")
        self.i += 9
        return Stream(info, blob)

    def _array(self) -> list:
        self.i += 1
        out = []
        while True:
            self.skip()
            if self.i < len(self.data) and self.data[self.i] == 93:
                self.i += 1
                return out
            out.append(self.parse())

    def _number_or_ref(self):
        start = self.i
        data = self.data
        i = self.i
        if data[i] in b"+-":
            i += 1
        saw_dot = False
        while i < len(data) and (48 <= data[i] <= 57 or data[i] == 46):
            if data[i] == 46:
                if saw_dot:
                    break
                saw_dot = True
            i += 1
        raw = data[start:i]
        self.i = i
        if not raw or raw in (b"+", b"-", b".", b"+.", b"-."):
            raise PdfError("bad number")
        number = float(raw) if saw_dot else int(raw)
        if saw_dot:
            return number
        saved = self.i
        self.skip()
        if self.i < len(data) and data[self.i] in b"+-0123456789":
            gen_start = self.i
            j = self.i
            if data[j] in b"+-":
                j += 1
            while j < len(data) and 48 <= data[j] <= 57:
                j += 1
            if j == gen_start or b"." in data[gen_start:j]:
                self.i = saved
                return number
            gen = int(data[gen_start:j])
            self.i = j
            self.skip()
            if self.data[self.i : self.i + 1] == b"R" and (
                self.i + 1 >= len(self.data) or self.data[self.i + 1] in b" \t\r\n\x0c\x00()<>[]{}/%"
            ):
                self.i += 1
                return Ref(int(number), gen)
        self.i = saved
        return number


def _read_line(data: bytes, i: int) -> tuple[bytes, int]:
    j = i
    while j < len(data) and data[j] not in b"\r\n":
        j += 1
    line = data[i:j]
    if j < len(data) and data[j] == 13:
        j += 1
        if j < len(data) and data[j] == 10:
            j += 1
    elif j < len(data) and data[j] == 10:
        j += 1
    return line, j


def _parse_classic_xref(data: bytes, i: int) -> tuple[dict, dict, int | None]:
    line, i = _read_line(data, i)
    if line.strip() != b"xref":
        raise PdfError("not an xref table")
    entries: dict = {}
    while True:
        parser = Parser(data, i)
        parser.skip()
        i = parser.i
        if data[i : i + 7] == b"trailer":
            parser.i = i + 7
            trailer = parser.parse()
            if not isinstance(trailer, dict):
                raise PdfError("bad trailer")
            prev = trailer.get("Prev")
            prev_i = int(prev) if isinstance(prev, (int, float)) else None
            return entries, trailer, prev_i
        line, i = _read_line(data, i)
        parts = line.split()
        if len(parts) != 2:
            raise PdfError("bad xref subsection")
        start, count = int(parts[0]), int(parts[1])
        for n in range(count):
            line, i = _read_line(data, i)
            bits = line.split()
            if len(bits) < 3:
                raise PdfError("bad xref entry")
            offset, gen, kind = int(bits[0]), int(bits[1]), bits[2]
            if kind == b"n":
                entries[start + n] = Offset(offset, gen)
    raise PdfError("xref did not end")


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_unpredict(data: bytes, columns: int) -> bytes:
    if columns <= 0:
        raise PdfError("bad predictor columns")
    row_in = columns + 1
    out = bytearray()
    prev = bytearray(columns)
    i = 0
    while i + row_in <= len(data):
        filt = data[i]
        cur = bytearray(data[i + 1 : i + row_in])
        if filt == 0:
            pass
        elif filt == 1:
            for j in range(columns):
                left = cur[j - 1] if j else 0
                cur[j] = (cur[j] + left) & 255
        elif filt == 2:
            for j in range(columns):
                cur[j] = (cur[j] + prev[j]) & 255
        elif filt == 3:
            for j in range(columns):
                left = cur[j - 1] if j else 0
                cur[j] = (cur[j] + ((left + prev[j]) // 2)) & 255
        elif filt == 4:
            for j in range(columns):
                left = cur[j - 1] if j else 0
                up = prev[j]
                up_left = prev[j - 1] if j else 0
                cur[j] = (cur[j] + _paeth(left, up, up_left)) & 255
        else:
            raise PdfError("bad png predictor")
        out += cur
        prev = cur
        i += row_in
    if i != len(data):
        raise PdfError("predictor length")
    return bytes(out)


def _decode_parms(parms) -> dict:
    if isinstance(parms, dict):
        return parms
    return {}


def _inflate(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error:
        return zlib.decompress(data, -15)


def decode_stream_bytes(info: dict, data: bytes) -> bytes:
    filt = info.get("Filter")
    parms = info.get("DecodeParms")
    if filt is None:
        filters = []
        parm_list = []
    elif isinstance(filt, list):
        filters = filt
        parm_list = parms if isinstance(parms, list) else [parms] * len(filters)
    else:
        filters = [filt]
        parm_list = [parms]
    out = data
    for filt, parm in zip(filters, parm_list):
        name = filt if isinstance(filt, str) else ""
        if name == "FlateDecode":
            out = _inflate(out)
            decoded = _decode_parms(parm)
            predictor = decoded.get("Predictor", 1)
            if isinstance(predictor, float):
                predictor = int(predictor)
            if predictor and predictor != 1:
                columns = decoded.get("Columns")
                if isinstance(columns, float):
                    columns = int(columns)
                if not isinstance(columns, int):
                    raise PdfError("predictor columns")
                if predictor >= 10:
                    out = _png_unpredict(out, columns)
                else:
                    raise PdfError("unsupported predictor")
        elif name == "ASCIIHexDecode":
            hexes = bytes(c for c in out if c not in b" \t\r\n\x0c")
            if hexes.endswith(b">"):
                hexes = hexes[:-1]
            if len(hexes) % 2:
                hexes += b"0"
            out = bytes.fromhex(hexes.decode("ascii"))
        else:
            raise PdfError(f"unsupported filter {name}")
    return out


def _xref_stream_entries(info: dict, raw: bytes) -> dict:
    data = decode_stream_bytes(info, raw)
    widths = info.get("W")
    if not isinstance(widths, list) or len(widths) != 3:
        raise PdfError("bad xref W")
    widths = [int(w) for w in widths]
    size = int(info.get("Size"))
    index = info.get("Index")
    if not isinstance(index, list):
        pairs = [(0, size)]
    else:
        nums = [int(n) for n in index]
        pairs = list(zip(nums[0::2], nums[1::2]))
    row_len = sum(widths)
    if row_len <= 0:
        raise PdfError("bad xref row")
    entries = {}
    p = 0
    for start, count in pairs:
        for n in range(count):
            row = data[p : p + row_len]
            if len(row) != row_len:
                raise PdfError("short xref stream")
            p += row_len
            fields = []
            o = 0
            for w in widths:
                if w == 0:
                    fields.append(0)
                else:
                    fields.append(int.from_bytes(row[o : o + w], "big"))
                o += w
            typ, f2, f3 = fields
            if widths[0] == 0:
                typ = 1
            num = start + n
            if typ == 1:
                entries[num] = Offset(f2, f3)
            elif typ == 2:
                entries[num] = InStream(f2, f3)
    return entries


def _read_xref_at(data: bytes, pos: int) -> tuple[dict, dict, int | None]:
    parser = Parser(data, pos)
    parser.skip()
    if data[parser.i : parser.i + 4] == b"xref":
        return _parse_classic_xref(data, parser.i)
    obj_num, gen, value = _parse_indirect(data, parser.i, resolver=None)
    if not isinstance(value, Stream):
        raise PdfError("xref is not a stream")
    info = value.info
    if info.get("Type") not in (None, "XRef"):
        raise PdfError("not an xref stream")
    entries = _xref_stream_entries(info, value.data)
    prev = info.get("Prev")
    prev_i = int(prev) if isinstance(prev, (int, float)) else None
    # The xref stream object is not listed inside itself until after we parse
    # the entries. Make sure its own offset is recorded if the stream says so.
    entries.setdefault(obj_num, Offset(parser.i, gen))
    trailer = info
    return entries, trailer, prev_i


def _parse_indirect(data: bytes, i: int, resolver):
    parser = Parser(data, i, resolver=resolver)
    num = parser.parse()
    gen = parser.parse()
    word = parser._word()
    if word != b"obj" or not isinstance(num, int) or not isinstance(gen, int):
        raise PdfError("not an indirect object")
    value = parser.parse()
    parser.skip()
    if parser.data[parser.i : parser.i + 6] == b"endobj":
        parser.i += 6
    return num, gen, value


class Document:
    def __init__(self, data: bytes):
        if data.find(b"%PDF-", 0, 1024) < 0:
            raise PdfError("not pdf")
        self.data = data
        start = data.rfind(b"startxref")
        if start < 0:
            raise PdfError("no startxref")
        i = start + len(b"startxref")
        while i < len(data) and data[i] in b" \t\r\n\x0c":
            i += 1
        line, _ = _read_line(data, i)
        try:
            xref_pos = int(line.strip())
        except ValueError as exc:
            raise PdfError("bad startxref") from exc
        self.xref_pos = xref_pos
        self.xref: dict = {}
        self.trailer: dict | None = None
        seen = set()
        pos = xref_pos
        while pos is not None and pos not in seen:
            seen.add(pos)
            entries, trailer, prev = _read_xref_at(data, pos)
            if self.trailer is None:
                self.trailer = trailer
            for num, ent in entries.items():
                self.xref.setdefault(num, ent)
            pos = prev
        if not self.trailer or not isinstance(self.trailer.get("Root"), Ref):
            raise PdfError("no root")
        if "Encrypt" in self.trailer:
            raise PdfError("encrypted")
        self.cache: dict = {}
        self.objstm_cache: dict = {}

    def resolve(self, item):
        if not isinstance(item, Ref):
            return item
        if item.num in self.cache:
            return self.cache[item.num]
        ent = self.xref.get(item.num)
        if isinstance(ent, Offset):
            _num, _gen, value = _parse_indirect(self.data, ent.offset, resolver=self.resolve)
            self.cache[item.num] = value
            return value
        if isinstance(ent, InStream):
            objects = self._object_stream(ent.stm_num)
            if item.num not in objects:
                raise PdfError("object missing from stream")
            self.cache[item.num] = objects[item.num]
            return objects[item.num]
        raise PdfError("dangling ref")

    def _object_stream(self, num: int) -> dict:
        if num in self.objstm_cache:
            return self.objstm_cache[num]
        value = self.resolve(Ref(num, 0))
        if not isinstance(value, Stream):
            raise PdfError("object stream is not a stream")
        info = value.info
        if info.get("Type") not in (None, "ObjStm"):
            raise PdfError("not an object stream")
        raw = decode_stream_bytes(info, value.data)
        count = int(info.get("N"))
        first = int(info.get("First"))
        header = raw[:first].decode("latin-1").split()
        if len(header) < count * 2:
            raise PdfError("bad object stream header")
        pairs = [(int(header[i]), int(header[i + 1])) for i in range(0, count * 2, 2)]
        found = {}
        for idx, (obj_num, rel) in enumerate(pairs):
            start = first + rel
            end = first + pairs[idx + 1][1] if idx + 1 < len(pairs) else len(raw)
            parser = Parser(raw, start, resolver=self.resolve)
            found[obj_num] = parser.parse()
        self.objstm_cache[num] = found
        return found


def _copy(value):
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _walk_pages(doc: Document, item, inherited: dict) -> list[tuple[int, int, dict]]:
    if isinstance(item, Ref):
        obj_num, gen = item.num, item.gen
        node = doc.resolve(item)
    else:
        raise PdfError("page node is direct")
    if not isinstance(node, dict):
        raise PdfError("page node is not a dict")
    typ = node.get("Type")
    kids = node.get("Kids")
    if typ == "Pages" or (isinstance(kids, list) and typ != "Page"):
        passed = dict(inherited)
        for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
            if key in node:
                passed[key] = node[key]
        pages = []
        for kid in kids:
            pages.extend(_walk_pages(doc, kid, passed))
        return pages
    if typ == "Page" or "Contents" in node or "MediaBox" in node:
        return [(obj_num, gen, node)]
    raise PdfError("unknown page node")


def _serialize(value) -> bytes:
    if isinstance(value, dict):
        parts = [b"<<"]
        for key, item in value.items():
            parts.append(b"/" + _encode_name(key) + b" " + _serialize(item))
        parts.append(b">>")
        return b" ".join(parts)
    if isinstance(value, list):
        return b"[" + b" ".join(_serialize(item) for item in value) + b"]"
    if isinstance(value, Ref):
        return f"{value.num} {value.gen} R".encode("ascii")
    if isinstance(value, PdfStr):
        return _literal(value.data)
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if value is None:
        return b"null"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value).encode("ascii")
    if isinstance(value, float):
        return (f"{value:.6f}".rstrip("0").rstrip(".")).encode("ascii")
    if isinstance(value, str):
        return b"/" + _encode_name(value)
    raise PdfError(f"cannot serialize {type(value).__name__}")


def _encode_name(name: str) -> bytes:
    raw = name.encode("latin-1")
    out = bytearray()
    for b in raw:
        if b in b"()<>[]{}/%# \t\r\n" or b < 33 or b > 126:
            out += f"#{b:02X}".encode("ascii")
        else:
            out.append(b)
    return bytes(out)


def _literal(data: bytes) -> bytes:
    out = bytearray(b"(")
    for b in data:
        if b in b"()\\":
            out += b"\\" + bytes([b])
        elif 32 <= b <= 126:
            out.append(b)
        else:
            out += f"\\{b:03o}".encode("ascii")
    out.append(41)
    return bytes(out)


def _overlay_stream(text: str) -> bytes:
    raw = text.encode("latin-1", "replace")
    # Keep the readable sentence in the content stream as literal text.
    # The middle dot is WinAnsi byte 0xB7 so pdftotext can show it.
    encoded = raw.replace("·".encode("latin-1", "replace"), b"\xb7")
    line = _literal(encoded)
    return b"q\nBT\n/GAVWM 9 Tf\n0.15 0.15 0.15 rg\n36 28 Td\n" + line + b" Tj\nET\nQ\n"


def _page_with_overlay(page: dict, inherited: dict, doc: Document, wm_num: int, font_num: int) -> dict:
    updated = _copy(page)
    contents = updated.get("Contents")
    wm = Ref(wm_num, 0)
    if contents is None:
        updated["Contents"] = wm
    elif isinstance(contents, Ref):
        updated["Contents"] = [contents, wm]
    elif isinstance(contents, list):
        updated["Contents"] = list(contents) + [wm]
    else:
        raise PdfError("bad contents")
    resources = updated.get("Resources")
    if resources is None:
        resources = inherited.get("Resources")
    if isinstance(resources, Ref):
        resources = doc.resolve(resources)
    if not isinstance(resources, dict):
        resources = {}
    resources = _copy(resources)
    fonts = resources.get("Font")
    if isinstance(fonts, Ref):
        fonts = doc.resolve(fonts)
    if not isinstance(fonts, dict):
        fonts = {}
    fonts = _copy(fonts)
    fonts["GAVWM"] = Ref(font_num, 0)
    resources["Font"] = fonts
    updated["Resources"] = resources
    return updated


def _max_obj(xref: dict) -> int:
    return max(xref) if xref else 0


def stamp_pdf(data: bytes, text: str) -> bytes:
    """Return a PDF whose every page paints ``text``. Never returns the original unchanged."""
    try:
        stamped = _stamp(data, text)
    except Exception:
        stamped = None
    marker = text.split("·", 1)[0].strip().encode("latin-1", "replace")
    if not marker:
        marker = b"Licensed exclusively"
    if stamped is None or not stamped.startswith(b"%PDF-") or stamped.count(marker) < 1:
        return minimal_license_pdf(text)
    return stamped


def _stamp(data: bytes, text: str) -> bytes:
    doc = Document(data)
    root = doc.trailer["Root"]
    catalog = doc.resolve(root)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("Pages"), Ref):
        raise PdfError("no pages")
    detailed = _walk_detailed(doc, catalog["Pages"], {})
    overlay = _overlay_stream(text)
    highest = _max_obj(doc.xref)
    font_num = highest + 1
    wm_num = font_num + 1
    out = bytearray(data)
    if not out.endswith(b"\n"):
        out += b"\n"
    offsets: dict[int, tuple[int, int]] = {}
    offsets[font_num] = (len(out), 0)
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    out += f"{font_num} 0 obj\n".encode() + font + b"\nendobj\n"
    for obj_num, gen, page, inherited in detailed:
        offsets[wm_num] = (len(out), 0)
        out += (
            f"{wm_num} 0 obj\n<< /Length {len(overlay)} >>\nstream\n".encode()
            + overlay
            + b"endstream\nendobj\n"
        )
        updated = _page_with_overlay(page, inherited, doc, wm_num, font_num)
        offsets[obj_num] = (len(out), gen)
        out += f"{obj_num} {gen} obj\n".encode() + _serialize(updated) + b"\nendobj\n"
        wm_num += 1
    xref_at = len(out)
    out += b"xref\n"
    for num in sorted(offsets):
        offset, gen = offsets[num]
        out += f"{num} 1\n".encode()
        out += f"{offset:010d} {gen:05d} n \n".encode()
    size = max(wm_num, highest + 1)
    root_ref = doc.trailer["Root"]
    out += b"trailer\n"
    out += (
        f"<< /Size {size} /Prev {doc.xref_pos} /Root {root_ref.num} {root_ref.gen} R >>\n".encode()
    )
    out += b"startxref\n"
    out += f"{xref_at}\n".encode()
    out += b"%%EOF\n"
    marker = text.encode("latin-1", "replace").split("·".encode())[0]
    # The overlay stores the middle dot as a raw WinAnsi byte, so count the
    # ASCII prefix that introduces the buyer identity.
    prefix = text.split("·", 1)[0].encode("latin-1", "replace")
    if out.count(prefix) < len(detailed):
        raise PdfError("overlay missing on a page")
    if marker and False:
        pass
    return bytes(out)


def _walk_detailed(doc: Document, item, inherited: dict) -> list[tuple[int, int, dict, dict]]:
    if not isinstance(item, Ref):
        raise PdfError("page node is direct")
    node = doc.resolve(item)
    if not isinstance(node, dict):
        raise PdfError("page node is not a dict")
    typ = node.get("Type")
    kids = node.get("Kids")
    if typ == "Pages" or (isinstance(kids, list) and typ != "Page"):
        passed = dict(inherited)
        for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
            if key in node:
                passed[key] = node[key]
        found = []
        for kid in kids:
            found.extend(_walk_detailed(doc, kid, passed))
        return found
    if typ == "Page" or "Contents" in node or "MediaBox" in node:
        return [(item.num, item.gen, node, inherited)]
    raise PdfError("unknown page node")


def minimal_license_pdf(text: str) -> bytes:
    """A one-page PDF that contains only the license line."""
    overlay = _overlay_stream(text)
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /GAVWM 5 0 R >> >> >>"
        ),
        4: f"<< /Length {len(overlay)} >>\nstream\n".encode() + overlay + b"endstream",
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    return _assemble(objects, root=1)


def _assemble(objects: dict[int, bytes], root: int) -> bytes:
    parts = [b"%PDF-1.4\n"]
    offsets = {}
    for num in sorted(objects):
        offsets[num] = sum(len(part) for part in parts)
        body = objects[num]
        parts.append(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_at = sum(len(part) for part in parts)
    size = max(objects) + 1
    xref = [f"xref\n0 {size}\n".encode(), b"0000000000 65535 f \n"]
    for num in range(1, size):
        xref.append(f"{offsets[num]:010d} 00000 n \n".encode())
    trailer = (
        f"trailer\n<< /Size {size} /Root {root} 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    )
    return b"".join(parts + xref + [trailer])


def page_content_texts(data: bytes) -> list[bytes]:
    """Decode each page's content streams and return them in order. For tests."""
    doc = Document(data)
    catalog = doc.resolve(doc.trailer["Root"])
    pages = _walk_detailed(doc, catalog["Pages"], {})
    texts = []
    for _num, _gen, page, _inherited in pages:
        contents = page.get("Contents")
        refs = contents if isinstance(contents, list) else [contents]
        chunks = []
        for ref in refs:
            value = doc.resolve(ref) if isinstance(ref, Ref) else ref
            if isinstance(value, Stream):
                chunks.append(decode_stream_bytes(value.info, value.data))
            elif isinstance(value, bytes):
                chunks.append(value)
        texts.append(b"\n".join(chunks))
    return texts
