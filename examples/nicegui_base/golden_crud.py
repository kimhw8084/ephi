"""Golden CRUD layering pattern: page -> service -> repository, never SQL in UI."""
from dataclasses import dataclass

from nicegui_base import Button, ButtonIntent, CrudPage, DataTable, Icons, LayoutSlot, TableColumn


@dataclass(frozen=True, slots=True)
class Equipment:
    equipment_id: str
    area: str
    owner: str


class EquipmentRepository:
    def list_equipment(self) -> tuple[Equipment, ...]:
        return (
            Equipment('ETCH-01', 'ETCH', 'A. Kim'),
            Equipment('CVD-04', 'CVD', 'J. Lee'),
        )


class EquipmentService:
    def __init__(self, repository: EquipmentRepository) -> None:
        self.repository = repository

    def rows(self) -> list[dict[str, str]]:
        return [
            {'equipment_id': item.equipment_id, 'area': item.area, 'owner': item.owner}
            for item in self.repository.list_equipment()
        ]


def build_page(service: EquipmentService | None = None) -> None:
    service = service or EquipmentService(EquipmentRepository())
    with CrudPage('Equipment', 'Search, inspect and manage equipment records.') as page:
        with page.slot(LayoutSlot.ACTIONS):
            Button('Add equipment', intent=ButtonIntent.PRIMARY, icon=Icons.ADD)
        with page.slot(LayoutSlot.DATA):
            DataTable(
                service.rows(),
                (
                    TableColumn('equipment_id', 'Equipment'),
                    TableColumn('area', 'Area'),
                    TableColumn('owner', 'Owner'),
                ),
                row_key='equipment_id',
                title='Equipment records',
            )


__all__ = ['Equipment', 'EquipmentRepository', 'EquipmentService', 'build_page']
