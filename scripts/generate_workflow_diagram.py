"""Generate the editable Excalidraw workflow and submission-ready SVG."""

from __future__ import annotations

import html
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
UPDATED = 1788271200000

STEPS = [
    (
        "1. 每日触发",
        ["Windows Task Scheduler", "AWS EventBridge Scheduler"],
        "#e8f1ee",
        "#285b4c",
    ),
    (
        "2. Agent 编排",
        ["Strands Agent", "@tool / OllamaModel", "固定工具边界"],
        "#e6eef6",
        "#315b7d",
    ),
    (
        "3. 官方资料采集",
        ["requests + BeautifulSoup", "OSHA SIR + News RSS", "SHA-256 不可变快照"],
        "#fff3d9",
        "#91651c",
    ),
    (
        "4. 增量入库与切片",
        ["SQLite UPSERT", "正文提取 + RAG 文档", "内容哈希去重"],
        "#f5e8ed",
        "#8a405b",
    ),
    (
        "5. 向量化与检索",
        ["Ollama nomic-embed", "pymilvus / Milvus", "AUTOINDEX + COSINE"],
        "#e9e6f4",
        "#5c5089",
    ),
    (
        "6. 工厂风险匹配",
        ["70% 语义 + 15% 危险词", "10% NAICS + 5% 后果", "hybrid-v1.0"],
        "#e5f0f4",
        "#356879",
    ),
    (
        "7. 只在需要时提醒",
        ["SQLite 审计视图", "Streamlit 决策台", "人工确认 / 忽略"],
        "#f8e7e4",
        "#9b3e35",
    ),
]


def _base(element_id: str, element_type: str, index: int) -> dict:
    return {
        "id": element_id,
        "type": element_type,
        "angle": 0,
        "strokeColor": "#243238",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "index": f"a{index:06d}",
        "roundness": None,
        "seed": 1200 + index * 97,
        "version": 1,
        "versionNonce": 2400 + index * 193,
        "isDeleted": False,
        "boundElements": [],
        "updated": UPDATED,
        "link": None,
        "locked": False,
    }


def _rectangle(element_id: str, index: int, x: int, color: str, stroke: str) -> dict:
    element = _base(element_id, "rectangle", index)
    element.update(
        {
            "x": x,
            "y": 245,
            "width": 280,
            "height": 260,
            "strokeColor": stroke,
            "backgroundColor": color,
            "roundness": {"type": 3},
        }
    )
    return element


def _text(
    element_id: str,
    index: int,
    x: int,
    y: int,
    width: int,
    height: int,
    value: str,
    size: int,
    color: str,
    *,
    align: str = "left",
) -> dict:
    element = _base(element_id, "text", index)
    element.update(
        {
            "x": x,
            "y": y,
            "width": width,
            "height": height,
            "strokeColor": color,
            "strokeWidth": 1,
            "roughness": 0,
            "fontSize": size,
            "fontFamily": 2,
            "text": value,
            "textAlign": align,
            "verticalAlign": "middle",
            "containerId": None,
            "originalText": value,
            "autoResize": False,
            "lineHeight": 1.35,
        }
    )
    return element


def _arrow(element_id: str, index: int, x: int) -> dict:
    element = _base(element_id, "arrow", index)
    element.update(
        {
            "x": x,
            "y": 375,
            "width": 54,
            "height": 0,
            "points": [[0, 0], [54, 0]],
            "lastCommittedPoint": None,
            "startBinding": None,
            "endBinding": None,
            "startArrowhead": None,
            "endArrowhead": "arrow",
            "elbowed": False,
        }
    )
    return element


def build_excalidraw() -> dict:
    elements: list[dict] = []
    index = 1
    elements.append(
        _text(
            "workflow-title",
            index,
            70,
            55,
            2170,
            62,
            "Hindsight Safety Agent：每日事故监控与本厂风险匹配工作流",
            42,
            "#162126",
        )
    )
    index += 1
    elements.append(
        _text(
            "workflow-subtitle",
            index,
            72,
            125,
            2100,
            40,
            "每一步都复用成熟组件；项目代码只实现事故证据、工厂风险和可解释评分的业务契约",
            22,
            "#59676d",
        )
    )
    index += 1

    for step_index, (title, lines, fill, stroke) in enumerate(STEPS):
        x = 70 + step_index * 320
        elements.append(_rectangle(f"step-{step_index + 1}", index, x, fill, stroke))
        index += 1
        elements.append(
            _text(
                f"step-title-{step_index + 1}",
                index,
                x + 22,
                270,
                236,
                56,
                title,
                23,
                stroke,
                align="center",
            )
        )
        index += 1
        elements.append(
            _text(
                f"step-body-{step_index + 1}",
                index,
                x + 24,
                340,
                232,
                130,
                "\n".join(lines),
                17,
                "#243238",
                align="center",
            )
        )
        index += 1
        if step_index < len(STEPS) - 1:
            elements.append(_arrow(f"arrow-{step_index + 1}", index, x + 286))
            index += 1

    elements.append(
        _rectangle("observability", index, 640, "#edf0f1", "#536168")
    )
    elements[-1].update({"y": 610, "width": 1100, "height": 125})
    elements[-1]["strokeStyle"] = "dashed"
    index += 1
    elements.append(
        _text(
            "observability-text",
            index,
            680,
            632,
            1020,
            80,
            "全链路可观测性（不改变业务流程）\nStrandsTelemetry → OpenTelemetry OTLP → Jaeger / AgentCore Observability",
            20,
            "#36454b",
            align="center",
        )
    )
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "https://excalidraw.com",
        "elements": elements,
        "appState": {
            "gridSize": None,
            "viewBackgroundColor": "#fbfcfc",
        },
        "files": {},
    }


def build_svg() -> str:
    width, height = 2380, 820
    chunks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<defs>",
        '<marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" '
        'orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#59676d"/></marker>',
        "</defs>",
        '<rect width="100%" height="100%" fill="#fbfcfc"/>',
        '<text x="70" y="86" font-family="Arial, Microsoft YaHei, sans-serif" '
        'font-size="42" font-weight="700" fill="#162126">Hindsight Safety Agent：每日事故监控与本厂风险匹配工作流</text>',
        '<text x="72" y="135" font-family="Arial, Microsoft YaHei, sans-serif" '
        'font-size="21" fill="#59676d">每一步都复用成熟组件；项目代码只实现事故证据、工厂风险和可解释评分的业务契约</text>',
    ]
    for index, (title, lines, fill, stroke) in enumerate(STEPS):
        x = 70 + index * 320
        chunks.append(
            f'<rect x="{x}" y="245" width="280" height="260" rx="7" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
        )
        chunks.append(
            f'<text x="{x + 140}" y="293" text-anchor="middle" '
            f'font-family="Arial, Microsoft YaHei, sans-serif" font-size="23" '
            f'font-weight="700" fill="{stroke}">{html.escape(title)}</text>'
        )
        for line_index, line in enumerate(lines):
            chunks.append(
                f'<text x="{x + 140}" y="{365 + line_index * 42}" text-anchor="middle" '
                f'font-family="Arial, Microsoft YaHei, sans-serif" font-size="17" '
                f'fill="#243238">{html.escape(line)}</text>'
            )
        if index < len(STEPS) - 1:
            chunks.append(
                f'<line x1="{x + 287}" y1="375" x2="{x + 314}" y2="375" '
                'stroke="#59676d" stroke-width="3" marker-end="url(#arrow)"/>'
            )
    chunks.extend(
        [
            '<rect x="640" y="610" width="1100" height="125" rx="7" fill="#edf0f1" '
            'stroke="#536168" stroke-width="2" stroke-dasharray="9 7"/>',
            '<text x="1190" y="660" text-anchor="middle" '
            'font-family="Arial, Microsoft YaHei, sans-serif" font-size="20" '
            'font-weight="600" fill="#36454b">全链路可观测性（不改变业务流程）</text>',
            '<text x="1190" y="700" text-anchor="middle" '
            'font-family="Arial, Microsoft YaHei, sans-serif" font-size="19" '
            'font-weight="600" fill="#36454b">StrandsTelemetry → OpenTelemetry OTLP → Jaeger / AgentCore Observability</text>',
            "</svg>",
        ]
    )
    return "\n".join(chunks)


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = [
        "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size=size)
    return ImageFont.load_default(size=size)


def build_png(path: Path) -> None:
    image = Image.new("RGB", (2380, 820), "#fbfcfc")
    draw = ImageDraw.Draw(image)
    draw.text(
        (70, 48),
        "Hindsight Safety Agent：每日事故监控与本厂风险匹配工作流",
        font=_font(42, bold=True),
        fill="#162126",
    )
    draw.text(
        (72, 116),
        "每一步都复用成熟组件；项目代码只实现事故证据、工厂风险和可解释评分的业务契约",
        font=_font(21),
        fill="#59676d",
    )
    for index, (title, lines, fill, stroke) in enumerate(STEPS):
        x = 70 + index * 320
        draw.rounded_rectangle(
            (x, 245, x + 280, 505),
            radius=7,
            fill=fill,
            outline=stroke,
            width=2,
        )
        draw.text(
            (x + 140, 282),
            title,
            font=_font(23, bold=True),
            fill=stroke,
            anchor="mm",
        )
        for line_index, line in enumerate(lines):
            draw.text(
                (x + 140, 365 + line_index * 42),
                line,
                font=_font(17),
                fill="#243238",
                anchor="mm",
            )
        if index < len(STEPS) - 1:
            start_x = x + 287
            draw.line((start_x, 375, start_x + 27, 375), fill="#59676d", width=3)
            draw.polygon(
                [(start_x + 27, 375), (start_x + 17, 369), (start_x + 17, 381)],
                fill="#59676d",
            )
    draw.rounded_rectangle(
        (640, 610, 1740, 735),
        radius=7,
        fill="#edf0f1",
        outline="#536168",
        width=2,
    )
    draw.text(
        (1190, 656),
        "全链路可观测性（不改变业务流程）",
        font=_font(20, bold=True),
        fill="#36454b",
        anchor="mm",
    )
    draw.text(
        (1190, 698),
        "StrandsTelemetry → OpenTelemetry OTLP → Jaeger / AgentCore Observability",
        font=_font(19, bold=True),
        fill="#36454b",
        anchor="mm",
    )
    image.save(path, format="PNG", optimize=True)


def main() -> int:
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "HINDSIGHT_WORKFLOW.excalidraw").write_text(
        json.dumps(build_excalidraw(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (DOCS / "hindsight-workflow.svg").write_text(build_svg(), encoding="utf-8")
    build_png(DOCS / "hindsight-workflow.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
