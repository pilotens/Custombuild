from __future__ import annotations


def allocate_bay_widths_um(
    carcass_inner_width_um: int,
    thickness_um: int,
    divider_count: int,
    ratios_ppm: tuple[int, ...] = (),
) -> tuple[int, ...]:
    """Allocate exact clear bay widths using deterministic integer arithmetic."""

    bay_count = divider_count + 1
    available_um = carcass_inner_width_um - divider_count * thickness_um
    if ratios_ppm and len(ratios_ppm) == bay_count:
        total = sum(ratios_ppm)
        widths = [(available_um * ratio) // total for ratio in ratios_ppm]
        remainder = available_um - sum(widths)
        order = sorted(
            range(bay_count),
            key=lambda index: (-(available_um * ratios_ppm[index] % total), index),
        )
        for index in order[:remainder]:
            widths[index] += 1
        return tuple(widths)
    base, remainder = divmod(available_um, bay_count)
    return tuple(base + (1 if index < remainder else 0) for index in range(bay_count))


def allocate_shelf_positions_um(
    bottom_surface_z_um: int,
    inner_height_um: int,
    thickness_um: int,
    shelf_count: int,
    ratios_ppm: tuple[int, ...] = (),
) -> tuple[int, ...]:
    """Allocate exact shelf bottom positions using the production compiler arithmetic."""

    if shelf_count == 0:
        return ()
    if ratios_ppm and len(ratios_ppm) == shelf_count:
        return tuple(
            bottom_surface_z_um
            + (inner_height_um * ratio + 500_000) // 1_000_000
            - thickness_um // 2
            for ratio in ratios_ppm
        )
    clear_total_um = inner_height_um - shelf_count * thickness_um
    opening_um, remainder = divmod(clear_total_um, shelf_count + 1)
    cursor_um = bottom_surface_z_um
    positions: list[int] = []
    for row in range(shelf_count):
        cursor_um += opening_um + (1 if row < remainder else 0)
        positions.append(cursor_um)
        cursor_um += thickness_um
    return tuple(positions)


def shelf_opening_heights_um(
    bottom_surface_z_um: int,
    inner_height_um: int,
    thickness_um: int,
    shelf_positions_um: tuple[int, ...],
) -> tuple[int, ...]:
    """Measure clear edge-to-edge openings around already allocated shelves."""

    cursor_um = bottom_surface_z_um
    openings: list[int] = []
    for position_um in shelf_positions_um:
        openings.append(position_um - cursor_um)
        cursor_um = position_um + thickness_um
    openings.append(bottom_surface_z_um + inner_height_um - cursor_um)
    return tuple(openings)
