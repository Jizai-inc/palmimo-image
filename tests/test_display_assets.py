from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import io
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


@pytest.mark.parametrize("fixture", ["happy_strokes", "heart_pulse", "shapes_move"])
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


def square(x: int, y: int, size: int, reverse: bool = False) -> dict[str, Any]:
    points = [[x, y], [x + size, y], [x + size, y + size], [x, y + size]]
    return {
        "ty": "sh",
        "ks": {"k": {"v": points[::-1] if reverse else points, "i": [[0, 0]] * 4, "o": [[0, 0]] * 4, "c": True}},
    }


def filled_at(blob: bytes, x: float, y: float) -> bool:
    draw_offset, draw_count = struct.unpack_from("<2I", blob, 40)
    track_offset = struct.unpack_from("<I", blob, 48)[0]
    key_offset = struct.unpack_from("<I", blob, 56)[0]
    for index in range(draw_count):
        _, kind, vertices, _, fill, track = struct.unpack_from("<5Ii", blob, draw_offset + index * 68)
        assert kind == 2 and fill
        dim, _, first, _ = struct.unpack_from("<3Ii", blob, track_offset + track * 16)
        values_offset = struct.unpack_from("<I", blob, key_offset + first * 32 + 8)[0]
        values = struct.unpack_from(f"<{dim}f", blob, values_offset)
        assert not any(values[vertices * 2 :])
        points = list(zip(values[: vertices * 2 : 2], values[1 : vertices * 2 : 2], strict=True))
        inside = False
        for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1], strict=True):
            if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
                inside = not inside
        if inside:
            return True
    return False


@pytest.mark.parametrize("paint_kind", ["fl", "gf"])
@pytest.mark.parametrize("reverse,rule,hole", [(True, 1, True), (False, 1, False), (False, 2, True)])
def test_shared_fill_nested_contours_preserve_fill_rule(
    tmp_path: Path, reverse: bool, rule: int, hole: bool, paint_kind: str
) -> None:
    doc = composition()
    paint = doc["layers"][0]["shapes"][1]
    if paint_kind == "gf":
        paint = gradient()
    paint["r"] = rule
    doc["layers"][0]["shapes"] = [square(20, 20, 100), square(40, 40, 40, reverse), paint]
    code, result, output = convert_json(tmp_path, doc)
    assert code == 0, result
    assert filled_at(output.read_bytes(), 30, 50)
    assert filled_at(output.read_bytes(), 55, 55) is not hole
    assert not filled_at(output.read_bytes(), 150, 50)


def test_shared_fill_intersecting_contours_are_rejected(tmp_path: Path) -> None:
    doc = composition()
    paint = doc["layers"][0]["shapes"][1]
    doc["layers"][0]["shapes"] = [square(20, 20, 100), square(80, 80, 100), paint]
    code, result, output = convert_json(tmp_path, doc)
    assert code == 2
    assert not output.exists()
    assert any(e["path"] == "$.layers[0].shapes[0]" for e in result["errors"])


@pytest.mark.parametrize("paint_kind", ["fl", "gf"])
def test_shared_fill_disjoint_contours_keep_separate_draws(tmp_path: Path, paint_kind: str) -> None:
    doc = composition()
    paint = doc["layers"][0]["shapes"][1]
    if paint_kind == "gf":
        paint = gradient()
    doc["layers"][0]["shapes"] = [square(20, 20, 40), square(100, 20, 40), paint]
    code, result, output = convert_json(tmp_path, doc)
    assert code == 0, result
    assert struct.unpack_from("<I", output.read_bytes(), 44)[0] == 2
    assert filled_at(output.read_bytes(), 30, 30)
    assert filled_at(output.read_bytes(), 110, 30)
    assert not filled_at(output.read_bytes(), 80, 30)


@pytest.mark.parametrize("crossing", [False, True])
def test_shared_fill_animation_requires_disjoint_motion_bounds(tmp_path: Path, crossing: bool) -> None:
    doc = composition()
    left = doc["layers"][0]["shapes"][0]
    left["p"] = {"a": 1, "k": [{"t": 0, "s": [50, 100]}, {"t": 30, "s": [100, 100]}]}
    left["s"] = {"k": [20, 20]}
    right = copy.deepcopy(left)
    right["p"] = {"a": 1, "k": [{"t": 0, "s": [200, 100]}, {"t": 30, "s": [50 if crossing else 250, 100]}]}
    paint = doc["layers"][0]["shapes"][1]
    doc["layers"][0]["shapes"] = [left, right, paint]
    code, result, output = convert_json(tmp_path, doc)
    assert code == (2 if crossing else 0), result
    assert output.exists() is not crossing


@pytest.mark.parametrize("damage", ["curved", "open", "slot", "animated", "self-crossing", "budget"])
def test_shared_fill_unproven_merges_are_rejected(tmp_path: Path, damage: str) -> None:
    doc = composition()
    outer, inner = square(20, 20, 100), square(40, 40, 40, True)
    shape = outer["ks"]["k"]
    if damage == "curved":
        shape["o"][0] = [0, 20]
    elif damage == "open":
        shape["c"] = False
    elif damage == "slot":
        outer["ks"]["sid"] = "outline"
    elif damage == "animated":
        outer["ks"] = {"a": 1, "k": [{"t": 0, "s": [shape]}, {"t": 30, "s": [copy.deepcopy(shape)]}]}
    elif damage == "self-crossing":
        shape["v"][1], shape["v"][2] = shape["v"][2], shape["v"][1]
    elif damage == "budget":
        # Eight nested boundaries need 46 vertices after paired bridges.
        paint = doc["layers"][0]["shapes"][1]
        paint["r"] = 2
        doc["layers"][0]["shapes"] = [square(20 + i * 5, 20 + i * 5, 100 - i * 10) for i in range(8)] + [paint]
    if damage != "budget":
        doc["layers"][0]["shapes"] = [outer, inner, doc["layers"][0]["shapes"][1]]
    code, result, output = convert_json(tmp_path, doc)
    assert code == 2
    assert not output.exists()
    assert all(e["path"].startswith("$.layers[0].shapes[") and e["message"] for e in result["errors"])


def test_lottie_reports_all_unsupported_features(tmp_path: Path) -> None:
    doc = composition()
    layer = doc["layers"][0]
    layer.update(parent=0, masksProperties=[{}], ef=[{}])
    layer["ks"]["p"] = {"k": [0, 0], "x": "value"}
    layer["shapes"].extend([{"ty": "tm"}, {"ty": "gs"}, {"ty": "el"}])
    layer["shapes"][1]["r"] = 3
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
    for i, shape in enumerate(shapes[:-1]):
        shape["p"]["k"] = [20 + i * 40, 240]
        shape["s"]["k"] = [20, 20]
    assert (convert_json(tmp_path, doc)[0] == 0) is accepted


@pytest.mark.parametrize("tag", ["#glow", "#glow(r=0)", "#glow(r=14)", "#glow(r=bad)"])
def test_lottie_rejects_glow_tag(tmp_path: Path, tag: str) -> None:
    doc = composition()
    doc["layers"][0]["nm"] = f"eye {tag}"
    code, _, output = convert_json(tmp_path, doc)
    assert code == 2 and not output.exists()


@pytest.mark.parametrize("count", [2, 8, 9])
@pytest.mark.parametrize("animated", [False, True])
def test_lottie_preserves_stacked_strokes(tmp_path: Path, count: int, animated: bool) -> None:
    doc = composition()
    shapes = doc["layers"][0]["shapes"]
    strokes = [
        {"ty": "st", "lc": 2, "lj": 2, "w": {"k": 2 + i}, "o": {"k": 100}, "c": {"k": [1, 0, 0, 1]}}
        for i in range(count)
    ]
    if animated:
        strokes[0]["w"] = {"a": 1, "k": [{"t": 0, "s": [2]}, {"t": 30, "s": [5]}]}
        strokes[0]["o"] = {"a": 1, "k": [{"t": 0, "s": [100]}, {"t": 30, "s": [50]}]}
    shapes[:] = [shapes[0], *strokes]
    code, result, output = convert_json(tmp_path, doc)
    if count > 8:
        assert code == 2 and not output.exists()
        assert any(error["code"] == "limit" for error in result["errors"])
    else:
        assert code == 0, result
        inspected_code, inspected = run_cli("inspect", output)
        assert inspected_code == 0
        assert inspected["draws"] == {"fill": 0, "stroke": count}
        blob = output.read_bytes()
        draw_offset, draw_count, track_offset = struct.unpack_from("<3I", blob, 40)
        assert track_offset - draw_offset == draw_count * 68


@pytest.mark.parametrize("layout", ["crossing", "mixed", "nested", "limit"])
def test_shared_paints_preserve_lottie_draw_order(tmp_path: Path, layout: str) -> None:
    doc = composition()
    red = {"ty": "st", "lc": 2, "lj": 2, "w": {"k": 4}, "c": {"k": [1, 0, 0, 1]}}
    blue = {**red, "w": {"k": 20}, "c": {"k": [0, 0, 1, 1]}}
    horizontal: dict[str, Any] = {
        "ty": "sh",
        "ks": {"k": {"v": [[20, 100], [180, 100]], "i": [[0, 0]] * 2, "o": [[0, 0]] * 2, "c": False}},
    }
    vertical = copy.deepcopy(horizontal)
    vertical["ks"]["k"]["v"] = [[100, 20], [100, 180]]
    expected = [(0, 0, 20, (100, 20)), (0, 0, 20, (20, 100)), (0, 0, 4, (100, 20)), (0, 0, 4, (20, 100))]
    shapes = [horizontal, vertical, red, blue]
    if layout in ("mixed", "limit"):
        shapes = [square(20, 20, 40), square(100, 20, 40), red, gradient(), {"ty": "fl"}, blue]
        expected = [
            (fill, gradient_kind, width, point)
            for fill, gradient_kind, width in [(0, 0, 20), (1, 0, 0), (1, 1, 0), (0, 0, 4)]
            for point in [(100, 20), (20, 20)]
        ]
        if layout == "limit":
            shapes.insert(2, square(180, 20, 40))
    elif layout == "nested":
        green = {**red, "w": {"k": 8}, "c": {"k": [0, 1, 0, 1]}}
        shapes = [
            {"ty": "gr", "it": [horizontal, red]},
            {"ty": "gr", "it": [{"ty": "gr", "it": [vertical, green]}, {"ty": "tr", "p": {"k": [10, 0]}}]},
            blue,
        ]
        expected = [(0, 0, 20, (100, 20)), (0, 0, 20, (20, 100)), (0, 0, 8, (100, 20)), (0, 0, 4, (20, 100))]
    doc["layers"][0]["shapes"] = shapes
    code, result, output = convert_json(tmp_path, doc)
    if layout == "limit":
        assert code == 2 and not output.exists()
        assert any(e["code"] == "limit" for e in result["errors"])
        return
    assert code == 0, result
    assert run_cli("inspect", output)[0] == 0
    blob = output.read_bytes()
    offset, count = struct.unpack_from("<2I", blob, 40)
    actual = []
    colors = []
    nodes = []
    for i in range(count):
        draw = struct.unpack_from("<5I6i2I4i", blob, offset + i * 68)
        actual.append((draw[4], draw[11], track_values(blob, draw[10])[0][0], track_values(blob, draw[5])[0][:2]))
        colors.append(track_values(blob, draw[8])[0])
        nodes.append(draw[0])
    assert actual == expected
    if layout == "crossing":
        assert colors == [(0, 0, 1, 1)] * 2 + [(1, 0, 0, 1)] * 2
    if layout == "nested":
        assert nodes[0] == nodes[2] and nodes[1] == nodes[3] and nodes[0] != nodes[1]


@pytest.mark.parametrize("paint_kind", ["fl", "gf"])
def test_shared_fill_merge_preserves_surrounding_strokes(tmp_path: Path, paint_kind: str) -> None:
    doc = composition()
    paint: dict[str, Any] = gradient() if paint_kind == "gf" else {"ty": "fl"}
    paint["r"] = 2
    stroke = {"ty": "st", "lc": 2, "lj": 2, "w": {"k": 4}}
    doc["layers"][0]["shapes"] = [square(20, 20, 100), square(40, 40, 40, True), stroke, paint, stroke.copy()]
    code, result, output = convert_json(tmp_path, doc)
    assert code == 0, result
    assert run_cli("inspect", output)[1]["draws"] == {"fill": 1, "stroke": 4}
    blob = output.read_bytes()
    offset, count = struct.unpack_from("<2I", blob, 40)
    assert [struct.unpack_from("<I", blob, offset + i * 68 + 16)[0] for i in range(count)] == [0, 0, 1, 0, 0]


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


@pytest.mark.parametrize("duration", [None, 0, 100])
def test_single_frame_gif_converts_to_static_image(tmp_path: Path, duration: int | None) -> None:
    source, output = tmp_path / "input.gif", tmp_path / "asset.bin"
    image = Image.new("RGB", (2, 2), "red")
    if duration is None:
        image.save(source)
    else:
        image.save(source, duration=duration)
    code, result = run_cli("convert", source, "-o", output)
    assert code == 0, result
    assert struct.unpack_from("<H", output.read_bytes(), 52)[0] == 1
    assert struct.unpack_from("<I", output.read_bytes(), 72)[0] == 0
    assert result["kind"] == "image"
    assert "slow_gif" not in {warning["code"] for warning in result["warnings"]}
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
            values = {"offset": 0xFFFFFFFC, "reference": 0, "numeric": 0x7FC00000, "version": 3}
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
        {"ty": "st", "lc": 1, "lj": 2, "w": {"a": 1, "k": [{"t": 0, "s": [2]}]}},
        {"ty": "st", "lc": 2, "lj": 2, "w": {"k": 5}},
        {"ty": "tm"},
    ]
    code, result, _ = convert_json(tmp_path, doc)
    assert code == 2
    assert {"$.layers[0].shapes[3].ty", "$.layers[0].shapes[1].lc", "$.layers[0].shapes[0].ks"} <= {
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


@pytest.mark.parametrize(
    "frame_count,field,value", [(2, 54, 1), (2, 60, 1), (2, 64, 65), (2, 68, 0), (2, 72, 0), (1, 72, 1)]
)
def test_inspect_rejects_invalid_pimg_fields(tmp_path: Path, frame_count: int, field: int, value: int) -> None:
    source, output = tmp_path / "input.gif", tmp_path / "asset.bin"
    frames = [Image.new("RGB", (1, 1), color) for color in ("red", "green")[:frame_count]]
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


@pytest.mark.parametrize("budget", ["keys", "cumulative_keys", "tracks", "components"])
def test_lottie_stops_before_building_over_budget_tracks(budget: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without early rejection, hostile properties allocate complete tracks before the limit is checked.
    loader = importlib.machinery.SourceFileLoader("limited_converter", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    converter = importlib.util.module_from_spec(spec)
    loader.exec_module(converter)
    reads: list[int] = []

    class Key(dict[str, Any]):
        def __getitem__(self, name: str) -> Any:
            if name == "t":
                reads.append(1)
            return super().__getitem__(name)

    doc = composition()
    transform = doc["layers"][0]["ks"]
    if budget in ("keys", "cumulative_keys"):
        monkeypatch.setattr(converter, "MAX_KEYS", 2, raising=False)
        count = 3 if budget == "keys" else 2
        transform["p"] = {"a": 1, "k": [Key(t=i, s=[0, 0], e=[0, 0]) for i in range(count)]}
        expected = 0 if budget == "keys" else 2
        if budget == "cumulative_keys":
            transform["a"] = {"a": 1, "k": [Key(t=0, s=[0, 0], e=[0, 0])]}
    elif budget == "tracks":
        monkeypatch.setattr(converter, "MAX_TRACKS", 1, raising=False)
        transform["a"] = {"a": 1, "k": [Key(t=0, s=[0, 0], e=[0, 0])]}
        expected = 0
    else:
        monkeypatch.setattr(converter, "MAX_COMPONENTS", 2, raising=False)
        transform["p"] = {"a": 1, "k": [Key(t=0, s=[0, 0, 0], e=[0, 0, 0])]}
        expected = 0
    with pytest.raises(converter.AssetError) as error:
        converter.Lottie(doc).convert()
    assert error.value.errors[0]["code"] == "limit"
    assert len(reads) == expected


@pytest.mark.parametrize("budget", ["bytes", "tokens", "depth"])
def test_lottie_source_limits_precede_json_decoding(budget: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Without bounded loading, a compact JSON source expands into a huge tree before conversion rejects it.
    loader = importlib.machinery.SourceFileLoader("bounded_converter", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    converter = importlib.util.module_from_spec(spec)
    loader.exec_module(converter)
    if budget == "bytes":
        monkeypatch.setattr(converter, "MAX_SOURCE_BYTES", 8, raising=False)
        raw = b'"123456789"'
    elif budget == "tokens":
        monkeypatch.setattr(converter, "MAX_JSON_TOKENS", 3, raising=False)
        raw = b"[0,0,0,0]"
    else:
        monkeypatch.setattr(converter, "MAX_JSON_DEPTH", 2, raising=False)
        raw = b"[[[0]]]"

    def decode(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("over-budget source reached JSON decoding")

    monkeypatch.setattr(converter.json, "loads", decode)
    with pytest.raises(converter.AssetError) as error:
        converter.load_lottie(io.BytesIO(raw))
    assert error.value.errors[0]["code"] == "limit"


def test_lottie_source_limits_accept_strings_across_read_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without this, escaped quotes or brackets inside strings can reject a valid bounded source.
    loader = importlib.machinery.SourceFileLoader("string_converter", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    converter = importlib.util.module_from_spec(spec)
    loader.exec_module(converter)
    doc = {"label": '"\\ ][ { escaped', "values": [0, True, None]}
    raw = json.dumps(doc).encode()

    class Chunked(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            return super().read(min(size, 3) if size is not None and size >= 0 else 3)

    monkeypatch.setattr(converter, "MAX_SOURCE_BYTES", len(raw))
    monkeypatch.setattr(converter, "MAX_JSON_TOKENS", 8)
    monkeypatch.setattr(converter, "MAX_JSON_DEPTH", 2)
    assert converter.load_lottie(Chunked(raw)) == doc


def gradient() -> dict[str, Any]:
    return {
        "ty": "gf",
        "t": 1,
        "g": {"p": 2, "k": {"k": [0, 1, 0, 0, 1, 0, 0, 1]}},
        "s": {"k": [20, 30]},
        "e": {"k": [100, 130]},
        "o": {"k": 75},
    }


def track_values(blob: bytes, index: int) -> list[tuple[float, ...]]:
    tracks, keys = (struct.unpack_from("<I", blob, p)[0] for p in (48, 56))
    dim, count, first, _ = struct.unpack_from("<3Ii", blob, tracks + index * 16)
    return [
        struct.unpack_from(f"<{dim}f", blob, struct.unpack_from("<I", blob, keys + k * 32 + p)[0])
        for k in range(first, first + count)
        for p in (8, 12)
    ]


@pytest.mark.parametrize("n", [1, 2, 16])
@pytest.mark.parametrize("kind,alpha,animated", [(1, False, False), (2, True, False), (1, True, True)])
def test_gradient_conversion_can_be_inspected(tmp_path: Path, kind: int, alpha: bool, animated: bool, n: int) -> None:
    doc = composition()
    paint = gradient()
    paint["t"] = kind
    paint["h"] = {"a": 0, "k": 0}
    paint["g"]["p"] = n
    stops = [v for i in range(n) for v in (int(i == n - 1), int(i == 0), 0, int(i != 0))]
    if alpha:
        m = 40 if n == 16 else 2
        stops.extend(v for i in range(m) for v in (int(i == m - 1), int(i == 0)))
    paint["g"]["k"] = {"k": stops}
    if animated:
        changed = [*stops]
        changed[1:4] = [0, 1, 0]
        paint["g"]["k"] = {"a": 1, "k": [{"t": 0, "s": stops}, {"t": 30, "s": changed}]}
        paint["s"] = {"a": 1, "k": [{"t": 0, "s": [20, 30, 0]}, {"t": 30, "s": [25, 35, 0]}]}
        paint["e"] = {"a": 1, "k": [{"t": 0, "s": [100, 130]}, {"t": 30, "s": [150, 180]}]}
    doc["layers"][0]["shapes"][1] = paint
    code, result, output = convert_json(tmp_path, doc)
    assert code == 0, result
    assert run_cli("inspect", output) == (0, result)
    blob = output.read_bytes()
    assert struct.unpack_from("<I", blob, 4)[0] == 2
    draw = struct.unpack_from("<5I6i2I4i", blob, struct.unpack_from("<I", blob, 40)[0])
    assert draw[4] == 1
    assert draw[11:13] == (kind, n)
    assert track_values(blob, draw[8]) == [(1, 1, 1, 1)] * 2
    assert track_values(blob, draw[15])[0] == tuple(stops)
    assert track_values(blob, draw[13])[0] == (20, 30)
    assert track_values(blob, draw[14])[0] == (100, 130)
    if animated:
        assert track_values(blob, draw[13])[-1] == (25, 35)
        assert track_values(blob, draw[14])[-1] == (150, 180)
        assert track_values(blob, draw[15])[-1] == tuple(changed)


def test_inherited_gradient_keeps_declaring_coordinate_node(tmp_path: Path) -> None:
    doc = composition()
    shape = doc["layers"][0]["shapes"][0]
    doc["layers"][0]["shapes"] = [{"ty": "gr", "it": [shape, {"ty": "tr", "p": {"k": [50, 60]}}]}, gradient()]
    code, result, output = convert_json(tmp_path, doc)
    assert code == 0, result
    blob = output.read_bytes()
    draw = struct.unpack_from("<5I6i2I4i", blob, struct.unpack_from("<I", blob, 40)[0])
    nodes = struct.unpack_from("<I", blob, 32)[0]
    assert draw[0] != draw[16]
    assert struct.unpack_from("<i", blob, nodes + draw[0] * 40)[0] == draw[16]
    position = struct.unpack_from("<i", blob, nodes + draw[0] * 40 + 4)[0]
    assert track_values(blob, position)[0] == (50, 60)


@pytest.mark.parametrize(
    "damage",
    [
        "gs",
        "kind",
        "highlight",
        "animated-highlight",
        "g-slot",
        "k-slot",
        "s-slot",
        "e-slot",
        "count-zero",
        "count-large",
        "count-float",
        "short",
        "odd",
        "value",
        "descending-color",
        "descending-alpha",
        "changing",
        "key-end",
        "rule",
        "ordering",
    ],
)
def test_gradient_rejects_invalid_source(tmp_path: Path, damage: str) -> None:
    doc = composition()
    paint = gradient()
    if damage == "gs":
        paint["ty"] = "gs"
    elif damage == "kind":
        paint["t"] = 3
    elif damage == "highlight":
        paint["h"] = {"k": 1}
    elif damage == "animated-highlight":
        paint["h"] = {"a": 1, "k": [{"t": 0, "s": [0]}]}
    elif damage.endswith("slot"):
        target = {"g-slot": paint["g"], "k-slot": paint["g"]["k"], "s-slot": paint["s"], "e-slot": paint["e"]}[damage]
        target["sid"] = "gradient"
    elif damage.startswith("count"):
        paint["g"]["p"] = {"count-zero": 0, "count-large": 17, "count-float": 2.0}[damage]
    elif damage == "short":
        paint["g"]["k"]["k"] = [0, 1, 0, 0]
    elif damage == "odd":
        paint["g"]["k"]["k"].append(0)
    elif damage == "value":
        paint["g"]["k"]["k"][1] = 1.1
    elif damage == "descending-color":
        paint["g"]["k"]["k"][0:5:4] = [1, 0]
    elif damage == "descending-alpha":
        paint["g"]["k"]["k"] += [1, 1, 0, 0]
    elif damage in ("changing", "key-end"):
        stops = paint["g"]["k"]["k"]
        other = [*stops, 0, 1] if damage == "changing" else [1, *stops[1:4], 0, *stops[5:]]
        paint["g"]["k"] = {"a": 1, "k": [{"t": 0, "s": stops, "e": other}, {"t": 30, "s": stops}]}
    elif damage == "rule":
        paint["r"] = 3
    doc["layers"][0]["shapes"][1] = paint
    if damage == "ordering":
        doc["layers"][0]["shapes"].append(square(0, 0, 5))
    doc["layers"].append({"ty": 5, "nm": "unsupported", "ks": {}})
    code, result, output = convert_json(tmp_path, doc)
    assert code == 2 and not output.exists()
    assert any(e["path"].startswith("$.layers[0].shapes") for e in result["errors"])
    assert any(e["path"] == "$.layers[1].ty" for e in result["errors"])


def test_inspect_rejects_spaf_v1(tmp_path: Path) -> None:
    _, _, output = convert_json(tmp_path, composition())
    blob = bytearray(output.read_bytes())
    struct.pack_into("<I", blob, 4, 1)
    refresh_crc(blob)
    output.write_bytes(blob)
    code, result = run_cli("inspect", output)
    assert code == 2 and result["errors"][0]["code"] == "version"


@pytest.mark.parametrize(
    "damage",
    [
        "kind",
        "count",
        "fill",
        "start-ref",
        "end-dim",
        "stops-dim",
        "node",
        "slot",
        "value",
        "coordinate",
        "solid",
    ],
)
def test_inspect_rejects_invalid_gradient_records(tmp_path: Path, damage: str) -> None:
    doc = composition()
    doc["layers"][0]["shapes"][1] = gradient()
    if damage == "slot":
        doc["layers"][0]["shapes"][0]["p"]["sid"] = "origin"
    code, result, output = convert_json(tmp_path, doc)
    assert code == 0, result
    blob = bytearray(output.read_bytes())
    draw, tracks, keys = (struct.unpack_from("<I", blob, p)[0] for p in (40, 48, 56))
    start, end, stops = struct.unpack_from("<3i", blob, draw + 52)
    if damage in ("kind", "count", "fill", "node", "start-ref", "solid"):
        offset, value = {
            "kind": (44, 3),
            "count": (48, 17),
            "fill": (16, 0),
            "node": (64, 99),
            "start-ref": (52, -1),
            "solid": (44, 0),
        }[damage]
        struct.pack_into("<i", blob, draw + offset, value)
    elif damage in ("end-dim", "stops-dim"):
        struct.pack_into("<I", blob, tracks + (end if damage == "end-dim" else stops) * 16, 1)
    elif damage == "slot":
        struct.pack_into("<i", blob, tracks + start * 16 + 12, 0)
    else:
        index = stops if damage == "value" else start
        first = struct.unpack_from("<I", blob, tracks + index * 16 + 8)[0]
        value_offset = struct.unpack_from("<I", blob, keys + first * 32 + 12)[0]
        struct.pack_into("<f", blob, value_offset, 1.1 if damage == "value" else 1025)
    refresh_crc(blob)
    output.write_bytes(blob)
    assert run_cli("inspect", output)[0] == 2
