"""Golden NiceGUI Base dashboard pattern for coding agents.

Copy the architecture, not the sample business data. This example uses only
public NiceGUI Base APIs and keeps runtime, composition and data concerns explicit.
"""
from nicegui_base import (
    AxisSpec,
    AxisType,
    DashboardPage,
    Icons,
    LayoutSlot,
    LineChart,
    MetricCard,
    MetricStrip,
    NiceGUIRuntimeAdapter,
    RuntimeConfig,
    SeriesSpec,
    StatusIntent,
    TrendDirection,
)

_TREND = [84, 87, 86, 91, 93, 94, 96]


def build_page() -> None:
    with DashboardPage('Operations overview', 'Fast status, trend and exception scanning.') as page:
        with page.slot(LayoutSlot.METRICS):
            with MetricStrip():
                MetricCard('Availability', '99.4%', delta='+0.3 pp', trend=TrendDirection.UP, intent=StatusIntent.SUCCESS, icon=Icons.CHECK)
                MetricCard('Open alerts', 7, delta='-3', trend=TrendDirection.DOWN, intent=StatusIntent.SUCCESS, icon=Icons.WARNING)
                MetricCard('Throughput', '1,284', description='Units this shift', icon=Icons.CHART_LINE)
        with page.slot(LayoutSlot.PRIMARY):
            LineChart(
                'Throughput trend',
                (SeriesSpec('throughput', 'Throughput', _TREND, smooth=True),),
                x_axis=AxisSpec(kind=AxisType.CATEGORY, categories=('06:00', '08:00', '10:00', '12:00', '14:00', '16:00', '18:00')),
            )


def runtime() -> NiceGUIRuntimeAdapter:
    return NiceGUIRuntimeAdapter(RuntimeConfig('Golden Dashboard', require_storage_secret=False, show_browser=True))


def main() -> None:
    runtime().run(root=build_page)


if __name__ == '__main__':
    main()
