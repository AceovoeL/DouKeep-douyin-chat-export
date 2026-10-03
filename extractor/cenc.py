"""MPEG-CENC decryption for Douyin IM videos.

Douyin encrypts IM video samples with AES-128-CTR following the MPEG-CENC
spec (ISO/IEC 23001-7):
  - 16-byte AES-128 key in cj.video.skey
  - Per-sample 8-byte IV in the senc box (padded to 16 bytes, low half = counter)
  - Subsample encryption: alternating clear NAL headers + encrypted NAL payloads
  - The counter advances only through encrypted bytes within a sample

The encrypted file is a regular MP4 (ftyp/free/mdat/moov). The moov contains
senc/saiz/saio boxes with the encryption metadata. After in-place decryption
of the mdat samples it becomes a plain playable MP4.
"""
import struct
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend


_CONTAINERS = ('moov', 'trak', 'mdia', 'minf', 'stbl', 'edts', 'udta',
               'meta', 'mvex', 'moof', 'traf', 'dinf')


def parse_boxes(buf, start, end):
    """Yield (type, pos, header_len, total_size, content_bytes) for each box."""
    pos = start
    out = []
    while pos < end - 8:
        bs = struct.unpack('>I', buf[pos:pos+4])[0]
        bt = buf[pos+4:pos+8].decode(errors='replace')
        hl = 8
        if bs == 1:
            bs = struct.unpack('>Q', buf[pos+8:pos+16])[0]
            hl = 16
        if bs == 0:
            bs = end - pos
        if bs < 8:
            break
        out.append((bt, pos, hl, bs, buf[pos+hl:pos+bs]))
        pos += bs
    return out


def find_box(buf, start, end, path):
    """Walk box path, e.g. ['mdia','minf','stbl','senc']. Returns box tuple or None."""
    for box in parse_boxes(buf, start, end):
        if box[0] == path[0]:
            if len(path) == 1:
                return box
            return find_box(buf, box[1] + box[2], box[1] + box[3], path[1:])
    return None


def _parse_stsz(content):
    default_size = struct.unpack('>I', content[4:8])[0]
    sample_count = struct.unpack('>I', content[8:12])[0]
    if default_size != 0:
        return [default_size] * sample_count
    return [struct.unpack('>I', content[12+i*4:16+i*4])[0]
            for i in range(sample_count)]


def _parse_senc(content, iv_size=8):
    flags = struct.unpack('>I', b'\x00' + content[1:4])[0]
    sample_count = struct.unpack('>I', content[4:8])[0]
    has_sub = bool(flags & 0x000002)
    out = []
    p = 8
    for _ in range(sample_count):
        iv = content[p:p+iv_size]
        p += iv_size
        subs = []
        if has_sub:
            sub_count = struct.unpack('>H', content[p:p+2])[0]
            p += 2
            for _ in range(sub_count):
                clear = struct.unpack('>H', content[p:p+2])[0]
                prot = struct.unpack('>I', content[p+2:p+6])[0]
                subs.append((clear, prot))
                p += 6
        out.append({'iv': iv, 'subs': subs})
    return out


def _parse_stco(content):
    entry_count = struct.unpack('>I', content[4:8])[0]
    return [struct.unpack('>I', content[8+i*4:12+i*4])[0]
            for i in range(entry_count)]


def _parse_stsc(content):
    entry_count = struct.unpack('>I', content[4:8])[0]
    out = []
    for i in range(entry_count):
        fc = struct.unpack('>I', content[8+i*12:12+i*12])[0]
        spc = struct.unpack('>I', content[12+i*12:16+i*12])[0]
        sdi = struct.unpack('>I', content[16+i*12:20+i*12])[0]
        out.append((fc, spc, sdi))
    return out


def _sample_offsets(stsc_entries, stco_offsets, stsz_sizes):
    out = []
    sample_idx = 0
    for chunk_idx, chunk_offset in enumerate(stco_offsets, start=1):
        samples_per_chunk = stsc_entries[0][1]
        for fc, spc, sdi in stsc_entries:
            if fc <= chunk_idx:
                samples_per_chunk = spc
            else:
                break
        offset = chunk_offset
        for _ in range(samples_per_chunk):
            if sample_idx >= len(stsz_sizes):
                return out
            sz = stsz_sizes[sample_idx]
            out.append((offset, sz))
            offset += sz
            sample_idx += 1
    return out


def _aes_ctr_decrypt(key, iv16, ciphertext):
    c = Cipher(algorithms.AES(key), modes.CTR(iv16), backend=default_backend())
    return c.decryptor().update(ciphertext)


def _decrypt_sample(key, sample_data, senc_info):
    """CENC sample decryption: alternating clear / encrypted chunks.
    The AES-CTR counter advances continuously through encrypted bytes only."""
    iv = senc_info['iv']
    iv_full = iv.ljust(16, b'\x00')
    subs = senc_info['subs']

    if not subs:
        return _aes_ctr_decrypt(key, iv_full, sample_data)

    layout = []
    for clear, prot in subs:
        layout.append(('c', clear))
        layout.append(('p', prot))

    # Concatenate encrypted parts → one CTR stream → split back
    pos = 0
    protected_chunks = []
    for kind, length in layout:
        if kind == 'p':
            protected_chunks.append(sample_data[pos:pos+length])
        pos += length
    decrypted = _aes_ctr_decrypt(key, iv_full, b''.join(protected_chunks))

    # Interleave with clear bytes
    out = bytearray()
    pos = 0
    dec_pos = 0
    for kind, length in layout:
        if kind == 'c':
            out.extend(sample_data[pos:pos+length])
        else:
            out.extend(decrypted[dec_pos:dec_pos+length])
            dec_pos += length
        pos += length
    return bytes(out)


def _decrypt_track_inplace(buf, track_box_pos, track_box_end, key, iv_size=8):
    senc = find_box(buf, track_box_pos, track_box_end, ['mdia','minf','stbl','senc'])
    stsz = find_box(buf, track_box_pos, track_box_end, ['mdia','minf','stbl','stsz'])
    stsc = find_box(buf, track_box_pos, track_box_end, ['mdia','minf','stbl','stsc'])
    stco = find_box(buf, track_box_pos, track_box_end, ['mdia','minf','stbl','stco'])
    if not (senc and stsz and stsc and stco):
        return 0, f"missing box: senc={bool(senc)} stsz={bool(stsz)} stsc={bool(stsc)} stco={bool(stco)}"

    senc_entries = _parse_senc(senc[4], iv_size=iv_size)
    sizes = _parse_stsz(stsz[4])
    stsc_entries = _parse_stsc(stsc[4])
    stco_offsets = _parse_stco(stco[4])
    samples = _sample_offsets(stsc_entries, stco_offsets, sizes)
    if len(samples) != len(senc_entries):
        return 0, f"sample count mismatch: stsz={len(samples)} senc={len(senc_entries)}"

    for i, (offset, size) in enumerate(samples):
        sample_data = bytes(buf[offset:offset+size])
        plain = _decrypt_sample(key, sample_data, senc_entries[i])
        buf[offset:offset+size] = plain
    return len(samples), "ok"


def decrypt_cenc_mp4(encrypted_bytes: bytes, key_hex: str) -> bytes:
    """Decrypt a Douyin IM CENC-encrypted mp4 in memory. Returns plain mp4 bytes.

    Args:
        encrypted_bytes: raw mp4 bytes downloaded from the douyinvod CDN.
        key_hex: 32-char hex string (cj.video.skey, 16 bytes AES-128 key).

    Returns:
        Plain mp4 bytes — playable in browsers / decodable by ffmpeg.
    """
    key = bytes.fromhex(key_hex)
    if len(key) != 16:
        raise ValueError(f"key must be 16 bytes (got {len(key)})")

    buf = bytearray(encrypted_bytes)
    moov_box = next((b for b in parse_boxes(buf, 0, len(buf)) if b[0] == 'moov'), None)
    if not moov_box:
        raise ValueError("no moov box found")

    traks = [b for b in parse_boxes(buf, moov_box[1] + moov_box[2],
                                          moov_box[1] + moov_box[3])
             if b[0] == 'trak']
    if not traks:
        raise ValueError("no trak boxes in moov")

    for trak in traks:
        n, _ = _decrypt_track_inplace(buf, trak[1] + trak[2],
                                            trak[1] + trak[3], key)
        # If a track has no senc, skip silently — some tracks may be unencrypted
    return bytes(buf)
