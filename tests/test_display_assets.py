from __future__ import annotations

import copy
import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "platform/files/usr/local/libexec/palmimo/palmimo-display-assets"
FIXTURES = Path(__file__).parent / "fixtures/display-assets"


def run_cli(*args: str | Path) -> tuple[int, dict[str, Any]]:
    result = subprocess.run([sys.executable, str(CLI), *map(str, args), "--json"], capture_output=True, text=True)
    assert not result.stderr, result.stderr
    return result.returncode, json.loads(result.stdout)


def composition() -> dict[str, Any]:
    return {
        "w": 480,
        "h": 480,
        "fr": 30,
        "ip": 0,
        "op": 90,
        "layers": [
            {
                "ty": 4,
                "nm": "eye",
                "ip": 0,
                "op": 90,
                "ks": {},
                "shapes": [
                    {"ty": "el", "p": {"k": [240, 240]}, "s": {"k": [100, 80]}},
                    {"ty": "fl", "c": {"k": [1, 0, 0, 1], "sid": "eye_color"}, "o": {"k": 100}},
                ],
            }
        ],
    }


def convert_json(tmp_path: Path, doc: Any) -> tuple[int, dict[str, Any], Path]:
    source, output = tmp_path / "input.json", tmp_path / "asset.bin"
    source.write_text(json.dumps(doc))
    code, result = run_cli("convert", source, "-o", output)
    return code, result, output


@pytest.mark.parametrize("fixture", ["happy_glowtag", "happy_neon", "heart_pulse", "shapes_move"])
def test_lottie_conversion_can_be_inspected(tmp_path: Path, fixture: str) -> None:
    output = tmp_path / "asset.bin"
    code, converted = run_cli("convert", FIXTURES / f"{fixture}.json", "-o", output)
    assert code == 0, converted
    inspected_code, inspected = run_cli("inspect", output)
    assert inspected_code == 0
    assert {k: v for k, v in converted.items() if k != "warnings"} == {
        k: v for k, v in inspected.items() if k != "warnings"
    }
    blob = output.read_bytes()
    assert blob[:4] == b"SPAF"
    assert converted["kind"] == "vector"
    assert converted["bytes"] == len(blob)
    assert converted["crc32"] == f"{zlib.crc32(blob):08x}"


def test_lottie_slots_survive_conversion(tmp_path: Path) -> None:
    code, result, output = convert_json(tmp_path, composition())
    assert code == 0, result
    assert result["params"] == {"eye_color": {"dim": 4}}
    assert run_cli("inspect", output) == (0, result)


def test_lottie_reports_all_unsupported_features(tmp_path: Path) -> None:
    doc = composition()
    layer = doc["layers"][0]
    layer.update(parent=0, masksProperties=[{}], ef=[{}])
    layer["ks"]["p"] = {"k": [0, 0], "x": "value"}
    layer["shapes"].extend([{"ty": "tm"}, {"ty": "gf"}, {"ty": "el"}])
    layer["shapes"][1]["r"] = 2
    doc["layers"].append({"ty": 5, "nm": "caption", "ks": {}})
    code, result, output = convert_json(tmp_path, doc)
    assert code == 2
    assert not output.exists()
    paths = {e["path"] for e in result["errors"]}
    assert {
        "$.layers[0].parent",
        "$.layers[0].masksProperties",
        "$.layers[0].ef",
        "$.layers[0].ks.p.x",
        "$.layers[0].shapes[2].ty",
        "$.layers[0].shapes[3].ty",
        "$.layers[0].shapes[4]",
        "$.layers[0].shapes[1].r",
        "$.layers[1].ty",
    } <= paths
    assert all(e["code"] and e["message"] and e["path"] for e in result["errors"])


@pytest.mark.parametrize("sid", ["", "Upper", "has-dash", "a" * 33, "é", "a\x00b"])
def test_lottie_rejects_invalid_slot_ids(tmp_path: Path, sid: str) -> None:
    doc = composition()
    doc["layers"][0]["shapes"][1]["c"]["sid"] = sid
    assert convert_json(tmp_path, doc)[0] == 2


@pytest.mark.parametrize("doc", [None, [], 1, {}, {"layers": [None]}, {"layers": "bad"}])
def test_malformed_lottie_fails_without_traceback(tmp_path: Path, doc: Any) -> None:
    code, result, _ = convert_json(tmp_path, doc)
    assert code == 2
    assert result["ok"] is False
    assert result["errors"]


@pytest.mark.parametrize(
    "property_name,value,accepted",
    [
        ("s", [-200, 200], True),
        ("s", [200.01, 100], False),
        ("p", [-1024, 1024], True),
        ("p", [1024.01, 0], False),
        ("o", 100, True),
        ("o", -0.01, False),
        ("r", float("nan"), False),
    ],
)
def test_lottie_transform_range_boundaries(tmp_path: Path, property_name: str, value: Any, accepted: bool) -> None:
    doc = composition()
    doc["layers"][0]["ks"][property_name] = {"k": value}
    assert (convert_json(tmp_path, doc)[0] == 0) is accepted


@pytest.mark.parametrize("draws,accepted", [(8, True), (9, False)])
def test_lottie_enforces_draw_capacity(tmp_path: Path, draws: int, accepted: bool) -> None:
    doc = composition()
    shapes = doc["layers"][0]["shapes"]
    shapes[:] = [copy.deepcopy(shapes[0]) for _ in range(draws)] + [shapes[1]]
    assert (convert_json(tmp_path, doc)[0] == 0) is accepted


@pytest.mark.parametrize("radius,accepted", [(0, True), (128, True), (-1, False), (129, False), ("bad", False)])
def test_lottie_glow_range(tmp_path: Path, radius: Any, accepted: bool) -> None:
    doc = composition()
    doc["layers"][0]["nm"] = f"eye #glow(r={radius})"
    assert (convert_json(tmp_path, doc)[0] == 0) is accepted


@pytest.mark.parametrize("format_name", ["PNG", "JPEG"])
def test_image_is_letterboxed_masked_and_encoded_rgb565(tmp_path: Path, format_name: str) -> None:
    source, output = tmp_path / f"source.{format_name.lower()}", tmp_path / "asset.bin"
    Image.new("RGB", (240, 120), (255, 0, 0)).save(source, format=format_name)
    code, converted = run_cli("convert", source, "-o", output)
    assert code == 0, converted
    assert converted["kind"] == "image"
    assert run_cli("inspect", output) == (0, converted)
    blob = output.read_bytes()
    assert struct.unpack_from("<HH", blob, 4) == (2, 1)
    offset, words, scans = struct.unpack_from("<III", blob, 64)
    assert scans == 0
    stream = struct.unpack_from(f"<{words}H", blob, offset)
    pixels: list[int] = []
    i = 0
    while i < len(stream):
        control = stream[i]
        i += 1
        count = (control & 0x7FFF) + 1
        if control & 0x8000:
            pixels.extend([stream[i]] * count)
            i += 1
        else:
            pixels.extend(stream[i : i + count])
            i += count
    assert len(pixels) == 480 * 480
    assert pixels[240 * 480 + 240] == 0xF800
    assert pixels[80 * 480 + 240] == 0
    assert pixels[120 * 480] == 0
    assert pixels[120 * 480 + 240] == 0xF800


@pytest.mark.parametrize("delays,expected", [([0, 10, 20, 30, 50, 100], [1, 1, 1, 2, 3, 6]), ([30, 30], [2, 2])])
def test_gif_delays_are_panel_refresh_counts(tmp_path: Path, delays: list[int], expected: list[int]) -> None:
    source, output = tmp_path / "input.gif", tmp_path / "asset.bin"
    frames = [Image.new("RGB", (4, 4), (i * 30, 0, 0)) for i in range(len(delays))]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=delays, loop=0)
    code, result = run_cli("convert", source, "-o", output)
    assert code == 0, result
    blob = output.read_bytes()
    assert [struct.unpack_from("<I", blob, 72 + i * 12)[0] for i in range(len(delays))] == expected
    assert result["kind"] == "gif"
    assert ("slow_gif" in {w["code"] for w in result["warnings"]}) is (max(expected) > 2)
    assert run_cli("inspect", output) == (0, result)


def test_single_frame_gif_without_delay_remains_animation(tmp_path: Path) -> None:
    source, output = tmp_path / "input.gif", tmp_path / "asset.bin"
    Image.new("RGB", (2, 2), "red").save(source)
    code, result = run_cli("convert", source, "-o", output)
    assert code == 0, result
    assert struct.unpack_from("<I", output.read_bytes(), 72)[0] == 1
    assert result["kind"] == "gif"
    assert run_cli("inspect", output) == (0, result)


def refresh_crc(blob: bytearray) -> None:
    if blob[:4] == b"SPAF":
        struct.pack_into("<I", blob, 16, 0)
        struct.pack_into("<I", blob, 16, zlib.crc32(blob))
    else:
        struct.pack_into("<I", blob, 12, zlib.crc32(blob[32:]))


@pytest.mark.parametrize("kind", ["vector", "image"])
@pytest.mark.parametrize("damage", ["truncated", "crc", "offset", "reference", "numeric", "version", "trailing"])
def test_inspect_rejects_corrupt_assets(tmp_path: Path, kind: str, damage: str) -> None:
    if kind == "vector":
        code, _, output = convert_json(tmp_path, composition())
    else:
        source, output = tmp_path / "input.png", tmp_path / "asset.bin"
        Image.new("RGB", (2, 2), "red").save(source)
        code, _ = run_cli("convert", source, "-o", output)
    assert code == 0
    blob = bytearray(output.read_bytes())
    if damage == "truncated":
        del blob[-1:]
    elif damage == "trailing":
        blob.extend(b"\x00\x00\x00\x00")
    elif damage == "crc":
        blob[-1] ^= 1
    else:
        if kind == "vector":
            positions = {"offset": 72, "reference": 80, "numeric": 20, "version": 4}
            values = {"offset": 0xFFFFFFFC, "reference": 0, "numeric": 0x7FC00000, "version": 2}
        else:
            positions = {"offset": 64, "reference": 68, "numeric": 52, "version": 4}
            values = {"offset": 0xFFFFFFFE, "reference": 1, "numeric": 65, "version": 3}
        struct.pack_into("<I", blob, positions[damage], values[damage])
        refresh_crc(blob)
    output.write_bytes(blob)
    code, result = run_cli("inspect", output)
    assert code == 2
    assert result["ok"] is False


@pytest.mark.parametrize("prefix", [b"", b"SPAF", b"PIMG", b"SPAF" + b"\0" * 75, b"PIMG" + b"\0" * 27])
def test_inspect_rejects_short_headers(tmp_path: Path, prefix: bytes) -> None:
    source = tmp_path / "bad.bin"
    source.write_bytes(prefix)
    assert run_cli("inspect", source)[0] == 2


def test_convert_failure_preserves_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "asset.bin"
    output.write_bytes(b"previous")
    source = tmp_path / "bad.json"
    source.write_text("{")
    code, result = run_cli("convert", source, "-o", output)
    assert code == 2
    assert result["errors"]
    assert output.read_bytes() == b"previous"


def test_unsupported_features_include_topology_and_stroke_errors(tmp_path: Path) -> None:
    doc = composition()
    doc["layers"][0]["shapes"] = [
        {"ty": "sh", "ks": {"k": {"v": [[0, 0]], "i": [[0, 0]], "o": [[0, 0]], "c": True}}},
        {"ty": "st", "lc": 2, "lj": 2, "w": {"a": 1, "k": [{"t": 0, "s": [2]}]}},
        {"ty": "st", "lc": 2, "lj": 2, "w": {"k": 5}},
        {"ty": "tm"},
    ]
    code, result, _ = convert_json(tmp_path, doc)
    assert code == 2
    assert {"$.layers[0].shapes[3].ty", "$.layers[0].shapes", "$.layers[0].shapes[0].ks"} <= {
        e["path"] for e in result["errors"]
    }


@pytest.mark.parametrize("vertices,accepted", [(2, True), (24, True), (25, False)])
def test_lottie_path_vertex_limits(tmp_path: Path, vertices: int, accepted: bool) -> None:
    doc = composition()
    doc["layers"][0]["shapes"][0] = {
        "ty": "sh",
        "ks": {"k": {"v": [[0, 0]] * vertices, "i": [[0, 0]] * vertices, "o": [[0, 0]] * vertices, "c": True}},
    }
    assert (convert_json(tmp_path, doc)[0] == 0) is accepted


@pytest.mark.parametrize("change", ["closure", "vertices", "end_vertices"])
def test_lottie_rejects_changing_path_topology(tmp_path: Path, change: str) -> None:
    doc = composition()
    start: dict[str, Any] = {"v": [[0, 0], [100, 0]], "i": [[0, 0], [0, 0]], "o": [[0, 0], [0, 0]], "c": True}
    end = copy.deepcopy(start)
    if change == "closure":
        end["c"] = False
    else:
        end["v"].append([100, 100])
    keys = [{"t": 0, "s": [start]}, {"t": 30, "s": [end]}]
    if change == "end_vertices":
        keys = [{"t": 0, "s": [start], "e": [end]}, {"t": 30, "s": [start]}]
    doc["layers"][0]["shapes"][0] = {"ty": "sh", "ks": {"a": 1, "k": keys}}
    assert convert_json(tmp_path, doc)[0] == 2


@pytest.mark.parametrize(
    "times,handle,accepted",
    [
        ([0, 30], 0.5, True),
        ([1, 1], 0.5, False),
        ([1, 1.000000001], 0.5, False),
        ([0, 30], -0.01, False),
        ([0, 30], 1.01, False),
    ],
)
def test_lottie_key_time_and_easing_limits(tmp_path: Path, times: list[float], handle: float, accepted: bool) -> None:
    doc = composition()
    doc["layers"][0]["ks"]["p"] = {
        "a": 1,
        "k": [
            {"t": times[0], "s": [0, 0], "o": {"x": handle, "y": 0}},
            {"t": times[1], "s": [100, 100], "h": 1},
        ],
    }
    assert (convert_json(tmp_path, doc)[0] == 0) is accepted


@pytest.mark.parametrize("depth,accepted", [(8, True), (9, False)])
def test_lottie_node_depth_limit(tmp_path: Path, depth: int, accepted: bool) -> None:
    doc = composition()
    shapes = doc["layers"][0]["shapes"]
    for _ in range(depth - 2):
        shapes = [{"ty": "gr", "it": shapes}]
    doc["layers"][0]["shapes"] = shapes
    assert (convert_json(tmp_path, doc)[0] == 0) is accepted


def test_lottie_slot_defaults_are_resolved(tmp_path: Path) -> None:
    doc = composition()
    doc["slots"] = {"eye_color": {"p": {"k": [0, 1, 0, 1]}}}
    code, result, output = convert_json(tmp_path, doc)
    assert code == 0, result
    blob = output.read_bytes()
    draw_offset = struct.unpack_from("<I", blob, 40)[0]
    color_track = struct.unpack_from("<i", blob, draw_offset + 32)[0]
    tracks_offset = struct.unpack_from("<I", blob, 48)[0]
    first_key = struct.unpack_from("<I", blob, tracks_offset + color_track * 16 + 8)[0]
    keys_offset = struct.unpack_from("<I", blob, 56)[0]
    value_offset = struct.unpack_from("<I", blob, keys_offset + first_key * 32 + 8)[0]
    assert struct.unpack_from("<4f", blob, value_offset) == (0, 1, 0, 1)


@pytest.mark.parametrize("frames,accepted", [(64, True), (65, False)])
def test_gif_frame_capacity(tmp_path: Path, frames: int, accepted: bool) -> None:
    images = [Image.new("RGB", (1, 1), (i, 0, 0)) for i in range(frames)]
    source = tmp_path / "input.gif"
    images[0].save(source, save_all=True, append_images=images[1:], duration=20)
    assert (run_cli("convert", source, "-o", tmp_path / "out.bin")[0] == 0) is accepted


@pytest.mark.parametrize("payload,suffix", [(b"GIF89a", ".gif"), (b"not an image", ".png"), (b"{}", ".txt")])
def test_convert_rejects_bad_source_content(tmp_path: Path, payload: bytes, suffix: str) -> None:
    source = tmp_path / f"input{suffix}"
    source.write_bytes(payload)
    assert run_cli("convert", source, "-o", tmp_path / "out.bin")[0] == 2


def test_convert_rejects_mismatched_extension(tmp_path: Path) -> None:
    source = tmp_path / "input.png"
    Image.new("RGB", (1, 1)).save(source, format="GIF")
    assert run_cli("convert", source, "-o", tmp_path / "out.bin")[0] == 2


@pytest.mark.parametrize("arguments", [[], ["inspect"], ["convert", "input.json"], ["unknown"]])
def test_cli_argument_errors_are_json(arguments: list[str]) -> None:
    code, result = run_cli(*arguments)
    assert code == 2
    assert result["ok"] is False
    assert result["errors"]


@pytest.mark.parametrize("keys,accepted", [(2033, True), (2034, False)])
def test_lottie_total_key_capacity(tmp_path: Path, keys: int, accepted: bool) -> None:
    doc = composition()
    doc["layers"][0]["ks"]["r"] = {"a": 1, "k": [{"t": i, "s": [0]} for i in range(keys)]}
    code, result, output = convert_json(tmp_path, doc)
    assert (code == 0) is accepted, result
    if accepted:
        assert struct.unpack_from("<I", output.read_bytes(), 60)[0] == 2048


@pytest.mark.parametrize("field,value", [(36, 33), (44, 9), (52, 257), (60, 2049), (68, 33)])
def test_inspect_rejects_excess_table_counts(tmp_path: Path, field: int, value: int) -> None:
    code, _, output = convert_json(tmp_path, composition())
    assert code == 0
    blob = bytearray(output.read_bytes())
    struct.pack_into("<I", blob, field, value)
    refresh_crc(blob)
    output.write_bytes(blob)
    assert run_cli("inspect", output)[0] == 2


@pytest.mark.parametrize("field,value", [(54, 1), (60, 1), (64, 65), (68, 0), (72, 0)])
def test_inspect_rejects_invalid_pimg_fields(tmp_path: Path, field: int, value: int) -> None:
    source, output = tmp_path / "input.gif", tmp_path / "asset.bin"
    frames = [Image.new("RGB", (1, 1), color) for color in ("red", "green")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=20)
    assert run_cli("convert", source, "-o", output)[0] == 0
    blob = bytearray(output.read_bytes())
    struct.pack_into("<H" if field == 54 else "<I", blob, field, value)
    refresh_crc(blob)
    output.write_bytes(blob)
    assert run_cli("inspect", output)[0] == 2


def test_inspect_rejects_rle_that_overflows_framebuffer(tmp_path: Path) -> None:
    # Eight maximum repeat runs expand to more pixels than the panel has.
    stream = struct.pack("<16H", *([0xFFFF, 0] * 8))
    body = struct.pack("<20sHHII", b"IMAGE", 1, 0, 64, 0) + struct.pack("<3I", 76, 16, 0) + stream
    blob = struct.pack("<4sHHII16x", b"PIMG", 2, 1, 32 + len(body), zlib.crc32(body)) + body
    source = tmp_path / "input.bin"
    source.write_bytes(blob)
    assert run_cli("inspect", source)[0] == 2


def test_oversized_image_header_fails_without_traceback(tmp_path: Path) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack("!I", len(payload)) + kind + payload + struct.pack("!I", zlib.crc32(kind + payload))

    header = struct.pack("!IIBBBBB", 20000, 20000, 8, 2, 0, 0, 0)
    source = tmp_path / "large.png"
    source.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b"")
    )
    assert run_cli("convert", source, "-o", tmp_path / "out.bin")[0] == 2


@pytest.mark.parametrize("truncated", [False, True])
def test_inspect_handles_maximum_literal_rle_runs(tmp_path: Path, truncated: bool) -> None:
    words = []
    for count in [32768] * 7 + [1024]:
        words.extend([count - 1, *([0xF800, 0x07E0] * (count // 2))])
    if truncated:
        words.pop()
    stream = struct.pack(f"<{len(words)}H", *words)
    body = struct.pack("<20sHHII", b"IMAGE", 1, 0, 64, 0) + struct.pack("<3I", 76, len(words), 0) + stream
    source = tmp_path / "literal.bin"
    source.write_bytes(struct.pack("<4sHHII16x", b"PIMG", 2, 1, 32 + len(body), zlib.crc32(body)) + body)
    assert (run_cli("inspect", source)[0] == 0) is not truncated
