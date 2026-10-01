#!/usr/bin/env python3
"""
Rewrite captured HEVC parameter sets to a different picture size.

Background: the SD card clips are 4K (3840x2160) but eufy's P2P live stream
only serves 1080p, so the camera will only ever hand us 1080p parameter sets.
Guessing the 4K configuration is hopeless because CTU size, min-CB size, profile,
RPS structure and the coding-tool flags must all match the bitstream exactly.

Instead, take the camera's genuine parameter sets and change only the geometry,
leaving every other field - and the entire trailing bit string - untouched. The
1080p->4K case is exactly 2x on both axes, so the min-CB unit convention does
not need to be known at all.

  python3 tools/rescale-sps.py IN.bin W H OUT.bin
"""
import sys


def rbsp_unescape(data):
    """Strip RBSP emulation-prevention bytes.

    HEVC inserts 0x03 after any 00 00 in the RBSP so the payload can never
    contain a start-code prefix. Those bytes are not part of the bitstream, and
    leaving them in shifts every subsequent field onto the wrong bit.
    """
    out = bytearray()
    zeros = 0
    for b in data:
        if zeros >= 2 and b == 0x03:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0x00 else 0
    return bytes(out)


def rbsp_escape(data):
    out = bytearray()
    zeros = 0
    for b in data:
        if zeros >= 2 and b <= 0x03:
            out.append(0x03)
            zeros = 0        # the inserted byte breaks the run
        out.append(b)
        zeros = zeros + 1 if b == 0x00 else 0
    return bytes(out)


class BitReader:
    def __init__(self, data):
        self.d, self.p = data, 0

    def u(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | ((self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1)
            self.p += 1
        return v

    def ue(self):
        z = 0
        while self.u(1) == 0:
            z += 1
            if z > 31:
                raise ValueError("malformed ue(v)")
        return (1 << z) - 1 + (self.u(z) if z else 0)

    def bit_pos(self):
        return self.p


class BitWriter:
    def __init__(self):
        self.bits = []

    def u(self, v, n):
        for i in range(n - 1, -1, -1):
            self.bits.append((v >> i) & 1)

    def ue(self, v):
        v += 1
        n = v.bit_length()
        self.u(0, n - 1)
        self.u(v, n)

    def copy_bits(self, data, nbits):
        for i in range(nbits):
            self.bits.append((data[i >> 3] >> (7 - (i & 7))) & 1)

    def to_bytes(self):
        b = self.bits[:]
        while len(b) % 8:
            b.append(0)
        out = bytearray()
        for i in range(0, len(b), 8):
            v = 0
            for j in range(8):
                v = (v << 1) | b[i + j]
            out.append(v)
        return bytes(out)


def split_nals(data):
    """Return [(offset, start_code_len, nal_type), ...].

    `k` is where the 00 00 01 pattern begins, so a 3-byte start code starts at
    k and a 4-byte one at k-1 (the extra leading 00).
    """
    out, j = [], 0
    while j < len(data) - 4:
        k = data.find(b"\x00\x00\x01", j)
        if k == -1:
            break
        sc = 4 if (k > 0 and data[k - 1] == 0) else 3
        out.append((k + 3 - sc, sc, (data[k + 3] >> 1) & 0x3F))
        j = k + 3
    return out


def rebuild_sps(rbsp, new_w, new_h):
    """Return a new SPS with pic_width/height replaced; everything else identical."""
    r = BitReader(rbsp)
    vps_id = r.u(4)
    max_sub = r.u(3)
    nesting = r.u(1)
    ptl_start = r.bit_pos()
    r.u(8); r.u(32); r.u(4); r.u(44); r.u(8)
    if max_sub > 0:
        for _ in range(max_sub):
            r.u(2)
        r.u(2 * (8 - max_sub))
        for _ in range(max_sub):
            r.u(88); r.u(8)
    ptl_end = r.bit_pos()
    if ptl_end % 8:
        r.u(8 - (ptl_end % 8))
    seq_id = r.ue()
    chroma = r.ue()
    separate = None
    if chroma == 3:
        separate = r.u(1)
    w = r.ue()
    h = r.ue()
    conf_flag = r.u(1)
    conf = [r.ue(), r.ue(), r.ue(), r.ue()] if conf_flag else []
    cut = r.bit_pos()  # everything after this is copied verbatim

    w2 = BitWriter()
    w2.u(vps_id, 4)
    w2.u(max_sub, 3)
    w2.u(nesting, 1)
    w2.copy_bits(rbsp, ptl_end - ptl_start) if False else None
    for i in range(ptl_start, ptl_end):
        w2.bits.append((rbsp[i >> 3] >> (7 - (i & 7))) & 1)
    w2.ue(seq_id)
    w2.ue(chroma)
    if separate is not None:
        w2.u(separate, 1)
    w2.ue(new_w)
    w2.ue(new_h)
    # conformance_window_flag is always present, even when it is 0.
    w2.u(1 if conf_flag else 0, 1)
    if conf_flag:
        for v in conf:
            w2.ue(v)
    for i in range(cut, len(rbsp) * 8):
        w2.bits.append((rbsp[i >> 3] >> (7 - (i & 7))) & 1)

    return w2.to_bytes(), {
        "level": rbsp[ptl_start // 8 + 11],
        "chroma": chroma,
        "old": (w, h),
        "new": (new_w, new_h),
    }


def main():
    if len(sys.argv) < 5:
        sys.exit(__doc__)
    src, tw, th, dst = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    data = open(src, "rb").read()
    nals = split_nals(data)
    print("input NAL types:", [t for _, _, t in nals])

    sps_i = next((i for i, (_, _, t) in enumerate(nals) if t == 33), None)
    if sps_i is None:
        sys.exit("no SPS (NAL type 33) in input")

    off, sc, _ = nals[sps_i]
    body_start = off + sc + 2                       # skip start code + NAL header
    end = nals[sps_i + 1][0] if sps_i + 1 < len(nals) else len(data)
    # trim the trailing rbsp_stop_one_bit + padding
    raw = data[body_start:end]
    rbsp = rbsp_unescape(raw)

    r = BitReader(rbsp)
    r.u(4); max_sub = r.u(3); r.u(1)
    ptl = r.bit_pos(); r.u(8); r.u(32); r.u(4); r.u(44); r.u(8)
    if max_sub > 0:
        for _ in range(max_sub):
            r.u(2)
        r.u(2 * (8 - max_sub))
        for _ in range(max_sub):
            r.u(88); r.u(8)
    r.ue()
    ch = r.ue()
    if ch == 3:
        r.u(1)
    ow, oh = r.ue(), r.ue()
    print(f"parsed: level_idc={rbsp[ptl//8+11]} chroma_idc={ch} pic={ow}x{oh}")

    if (tw * oh) != (th * ow):
        print(f"WARNING: {tw}x{th} is not the same aspect ratio as {ow}x{oh}; "
              "scaling non-uniformly can break the bitstream")
    new_sps, info = rebuild_sps(rbsp, tw, th)
    print(f"  {info['old']} -> {info['new']}  (level {info['level']}, chroma {info['chroma']})")

    out = bytearray()
    # Keep the start code AND the 2-byte NAL header (type/layer/tid) verbatim.
    out += data[:off + sc + 2]
    out += rbsp_escape(new_sps)
    out += data[end:]
    open(dst, "wb").write(bytes(out))
    print(f"wrote {dst} ({len(out)} bytes, was {len(data)})")


if __name__ == "__main__":
    main()
