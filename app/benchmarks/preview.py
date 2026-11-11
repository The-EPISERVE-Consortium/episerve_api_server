"""Static images of the target values frozen with a benchmark."""

import base64
from io import BytesIO
from threading import Lock

import pandas as pd
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator
from PIL import Image

from app.benchmarks.definition import Benchmark
from app.benchmarks.materialize import Built

_RENDER_LOCK = Lock()
_MAX_IMAGE_BYTES = 16_000
INPUT_COLOR = "#216dc2"
REFERENCE_COLOR = "#008895"


def _image(benchmark: Benchmark, built: Built, *, mobile: bool) -> str:
    """Render a compact PNG without pyplot or a display server.

    Args:
        benchmark: Frozen target and forecast window definition.
        built: Input and reference tables used by the benchmark.
        mobile: Whether to use a narrow layout with fewer date ticks.

    Returns:
        PNG data URI with bounded size for the dedicated preview column.
    """
    figure = Figure(figsize=(5 if mobile else 11, 3), dpi=110, facecolor="white")
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    figure.subplots_adjust(left=0.16 if mobile else 0.09, right=0.98, top=0.94, bottom=0.2)
    start, end = pd.Timestamp(benchmark.test_start), pd.Timestamp(benchmark.test_end)
    axes.axvspan(start, end, color=REFERENCE_COLOR, alpha=0.07, linewidth=0)
    for frame, color in ((built.input, INPUT_COLOR), (built.reference, REFERENCE_COLOR)):
        axes.plot(pd.to_datetime(frame[benchmark.date_column]), frame[benchmark.target_column],
                  color=color, linewidth=1.6, marker=".", markersize=3)
    axes.axvline(start, color=REFERENCE_COLOR, linewidth=0.8, linestyle="--", alpha=0.7)
    locator = AutoDateLocator(minticks=3, maxticks=3 if mobile else 6)
    axes.xaxis.set_major_locator(locator)
    axes.xaxis.set_major_formatter(ConciseDateFormatter(locator))
    axes.yaxis.set_major_locator(MaxNLocator(nbins=4))
    axes.ticklabel_format(axis="y", style="sci", scilimits=(-3, 4), useMathText=True)
    axes.tick_params(axis="both", labelsize=9, colors="#475569", length=0, pad=8)
    axes.yaxis.get_offset_text().set_fontsize(9)
    axes.yaxis.get_offset_text().set_color("#475569")
    axes.grid(axis="y", color="#e5e7eb", linewidth=0.7)
    axes.set_axisbelow(True)
    axes.margins(x=0.02, y=0.12)
    for name, spine in axes.spines.items():
        spine.set_visible(name == "bottom")
        spine.set_color("#d4cfc5")
    buffer = BytesIO()
    figure.savefig(buffer, format="png")
    figure.clear()
    image = Image.open(BytesIO(buffer.getvalue())).convert("RGB")
    while True:
        output = BytesIO()
        image.quantize(colors=64).save(output, format="PNG", optimize=True)
        data = output.getvalue()
        if len(data) <= _MAX_IMAGE_BYTES:
            return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        image = image.resize((int(image.width * 0.85), int(image.height * 0.85)), Image.Resampling.LANCZOS)


def build(benchmark: Benchmark, built: Built) -> dict:
    """Create desktop and mobile images from the exact finalized target tables.

    Args:
        benchmark: Definition containing the target column and held-out window.
        built: Generated model input and scoring reference tables.

    Returns:
        Versioned image metadata, including counts for the two observed series.
    """
    with _RENDER_LOCK:
        return {"version": 1, "image": _image(benchmark, built, mobile=False),
                "mobile_image": _image(benchmark, built, mobile=True),
                "input_points": len(built.input), "reference_points": len(built.reference)}
