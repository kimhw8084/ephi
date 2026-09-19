"""Golden v3 shared-data-session pattern: one authority for table, KPI and chart."""
from nicegui_base import (
    Aggregation,
    AxisSpec,
    AxisType,
    BarChart,
    ColumnKind,
    DataExplorerPage,
    DataSession,
    Dataset,
    Dimension,
    LayoutSlot,
    Metric,
    MetricCard,
    MetricStrip,
    SeriesSpec,
    TableColumn,
    DataTable,
)

_ROWS = (
    {'id': 'A-101', 'area': 'ETCH', 'tool': 'E01', 'output': 112, 'yield_pct': 98.7},
    {'id': 'A-102', 'area': 'ETCH', 'tool': 'E02', 'output': 104, 'yield_pct': 97.9},
    {'id': 'A-103', 'area': 'CVD', 'tool': 'C01', 'output': 121, 'yield_pct': 99.1},
    {'id': 'A-104', 'area': 'CVD', 'tool': 'C02', 'output': 118, 'yield_pct': 98.4},
)


def make_session() -> DataSession:
    dataset = Dataset(
        'production',
        _ROWS,
        row_key='id',
        dimensions=(Dimension('area'), Dimension('tool')),
        metrics=(Metric('output', aggregation=Aggregation.SUM), Metric('yield_pct', aggregation=Aggregation.AVG)),
    )
    return DataSession(dataset)


def build_page() -> None:
    session = make_session()
    rows = session.rows().rows
    by_area = session.aggregate(dimensions=('area',), metrics=('output',)).rows
    with DataExplorerPage('Production explorer', 'One DataSession drives all analytical surfaces.') as page:
        with page.slot(LayoutSlot.METRICS):
            with MetricStrip():
                MetricCard('Total output', session.metric('output'))
                MetricCard('Average yield', f"{session.metric('yield_pct'):.1f}%")
        with page.slot(LayoutSlot.PRIMARY):
            BarChart(
                'Output by area',
                (SeriesSpec('output', 'Output', [row['output'] for row in by_area]),),
                x_axis=AxisSpec(kind=AxisType.CATEGORY, categories=tuple(row['area'] for row in by_area)),
            )
        with page.slot(LayoutSlot.DATA):
            DataTable(
                rows,
                (
                    TableColumn('id', 'Record'),
                    TableColumn('area', 'Area'),
                    TableColumn('tool', 'Tool'),
                    TableColumn('output', 'Output', kind=ColumnKind.INTEGER),
                    TableColumn('yield_pct', 'Yield', kind=ColumnKind.PERCENT, decimals=1),
                ),
                row_key='id',
                title='Production records',
            )


__all__ = ['build_page', 'make_session']
