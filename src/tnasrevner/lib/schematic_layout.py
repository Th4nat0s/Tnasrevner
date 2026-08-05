"""Deterministic schematic placement optimization helpers."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,too-many-arguments,too-many-positional-arguments
# pylint: disable=too-many-locals,too-many-nested-blocks,too-few-public-methods
# pylint: disable=too-many-branches,too-many-lines

from collections.abc import Callable
import math

from PySide6.QtCore import QLineF, QThread, QPointF, QRectF, Signal

from ..project import Device


class _OptimizationCancelled(Exception):
    """Signal internal used to stop an optimizer without applying results."""


class SchematicLayoutOptimizer:
    """Search compact device positions while respecting fixed components."""

    _WIRE_LENGTH_WEIGHT = 8.0
    _LONG_WIRE_WEIGHT = 0.025
    _GRAPH_SPAN_WEIGHT = 3.0
    _LOCAL_PASSES = 3
    _LOCAL_OFFSETS = (
        (0.0, 0.0),
        (-220.0, 0.0),
        (220.0, 0.0),
        (0.0, -220.0),
        (0.0, 220.0),
        (-220.0, -220.0),
        (-220.0, 220.0),
        (220.0, -220.0),
        (220.0, 220.0),
    )

    @staticmethod
    def _is_compact_passive(device: Device) -> bool:
        """Return whether a symbol should use tight passive spacing.

        Args:
            device: Component to classify.

        Returns:
            True for resistor and capacitor references.
        """
        return device.reference.strip().casefold().startswith(("r", "c"))

    @staticmethod
    def _candidate_rotations(device: Device, current: float) -> tuple[float, ...]:
        """Return only electrically distinct candidate rotations.

        Args:
            device: Component being optimized.
            current: Current schematic rotation.

        Returns:
            Candidate rotations in degrees.
        """
        if len(device.pins) == 2:
            return (0.0, 90.0, 180.0, 270.0)
        return (current,)

    @classmethod
    def _passive_sibling_edges(
        cls, devices: list[Device], edges: set[tuple[str, str]]
    ) -> set[tuple[str, str]]:
        """Join passive branches having identical electrical neighbors.

        Args:
            devices: Components participating in layout.
            edges: Real electrical component edges.

        Returns:
            Layout-only cohesion edges between sibling passives.
        """
        adjacency = {device.device_id: set() for device in devices}
        for left_id, right_id in edges:
            adjacency[left_id].add(right_id)
            adjacency[right_id].add(left_id)
        passive_ids = sorted(
            device.device_id for device in devices if cls._is_compact_passive(device)
        )
        return {
            (left_id, right_id)
            for index, left_id in enumerate(passive_ids)
            for right_id in passive_ids[index + 1 :]
            if adjacency[left_id] and adjacency[left_id] == adjacency[right_id]
        }

    @classmethod
    def _spring_target_spacing(
        cls,
        left: Device,
        right: Device,
        left_size: tuple[float, float],
        right_size: tuple[float, float],
    ) -> float:
        """Return desired center spacing for one connected pair.

        Args:
            left: First connected component.
            right: Second connected component.
            left_size: Oriented size of first symbol.
            right_size: Oriented size of second symbol.

        Returns:
            Desired center-to-center distance.
        """
        if cls._is_compact_passive(left) and cls._is_compact_passive(right):
            return max(
                (left_size[0] + right_size[0]) / 2 + 32.0,
                (left_size[1] + right_size[1]) / 2 + 32.0,
            )
        return max(left_size[0], right_size[0]) / 2 + 240.0

    @classmethod
    def _spring_minimum_spacing(
        cls,
        left: Device,
        right: Device,
        left_size: tuple[float, float],
        right_size: tuple[float, float],
        delta: QPointF,
        distance: float,
    ) -> float:
        """Return non-overlapping center distance along current direction.

        Args:
            left: First nearby component.
            right: Second nearby component.
            left_size: Oriented size of first symbol.
            right_size: Oriented size of second symbol.
            delta: Vector from first center to second center.
            distance: Current center-to-center distance.

        Returns:
            Minimum safe center distance.
        """
        if not (cls._is_compact_passive(left) and cls._is_compact_passive(right)):
            return math.hypot(*left_size) / 2 + math.hypot(*right_size) / 2 + 80.0
        direction_x = abs(delta.x()) / distance
        direction_y = abs(delta.y()) / distance
        clear_x = ((left_size[0] + right_size[0]) / 2 + 8.0) / max(direction_x, 1e-6)
        clear_y = ((left_size[1] + right_size[1]) / 2 + 8.0) / max(direction_y, 1e-6)
        return min(clear_x, clear_y)

    @staticmethod
    def _oriented_size(
        device: Device,
        rotation: float,
        size_function: Callable[[Device], tuple[float, float]],
    ) -> tuple[float, float]:
        """Return a symbol size after a schematic quarter-turn.

        Args:
            device: Component whose dimensions are required.
            rotation: Schematic rotation in degrees.
            size_function: Function returning the unrotated symbol size.

        Returns:
            Width and height after rotation.
        """
        width, height = size_function(device)
        return (height, width) if round(rotation / 90.0) % 2 else (width, height)

    @staticmethod
    def _segments_cross(
        first: tuple[QPointF, QPointF], second: tuple[QPointF, QPointF]
    ) -> bool:
        """Return whether two graph edges have a proper interior crossing.

        Args:
            first: Endpoints of the first edge.
            second: Endpoints of the second edge.

        Returns:
            True when the segments cross away from their endpoints.
        """

        def orientation(a: QPointF, b: QPointF, c: QPointF) -> float:
            """Return the signed area of an oriented point triplet."""
            return (b.x() - a.x()) * (c.y() - a.y()) - (b.y() - a.y()) * (c.x() - a.x())

        first_a, first_b = first
        second_a, second_b = second
        return (
            orientation(first_a, first_b, second_a)
            * orientation(first_a, first_b, second_b)
            < 0
            and orientation(second_a, second_b, first_a)
            * orientation(second_a, second_b, first_b)
            < 0
        )

    @classmethod
    def _terminal_point(
        cls,
        device: Device,
        pin_index: int,
        positions: dict[str, QPointF],
        rotations: dict[str, float],
        size_function: Callable[[Device], tuple[float, float]],
    ) -> QPointF:
        """Return an approximate rendered endpoint for one component pin.

        Args:
            device: Component owning the pin.
            pin_index: Zero-based pin index.
            positions: Candidate component centers.
            rotations: Candidate component rotations.
            size_function: Function returning each unrotated symbol size.

        Returns:
            Approximate logical endpoint position.
        """
        center = positions[device.device_id]
        rotation = rotations[device.device_id]
        angle = math.radians(rotation)
        if len(device.pins) == 2:
            width, _height = cls._oriented_size(device, rotation, size_function)
            offset = -width / 2 if pin_index == 0 else width / 2
            return center + QPointF(offset * math.cos(angle), offset * math.sin(angle))
        if not device.reference.strip().casefold().startswith(("ic", "u")):
            return positions[device.device_id]
        width, height = size_function(device)
        side_pins = max(1, math.ceil(len(device.pins) / 4))
        side = min(pin_index // side_pins, 3)
        side_start = side * side_pins
        side_count = min(side_pins, len(device.pins) - side_start)
        slot = pin_index - side_start
        if side in {0, 2}:
            tangent = ((side_count - 1) / 2 - slot) * 36.0
            local = QPointF(-width / 2 if side == 0 else width / 2, tangent)
        else:
            tangent = (slot - (side_count - 1) / 2) * 36.0
            local = QPointF(tangent, -height / 2 if side == 1 else height / 2)
        return center + QPointF(
            local.x() * math.cos(angle) - local.y() * math.sin(angle),
            local.x() * math.sin(angle) + local.y() * math.cos(angle),
        )

    @classmethod
    def _align_passive_terminals(
        cls,
        devices: list[Device],
        positions: dict[str, QPointF],
        rotations: dict[str, float],
        connections: list[tuple[str, int, str, int]],
        world_size: float,
        size_function: Callable[[Device], tuple[float, float]],
    ) -> None:
        """Align outward passive terminals with exact connected IC pins.

        Args:
            devices: Components participating in layout.
            positions: Mutable component centers.
            rotations: Current component rotations.
            connections: Exact pin-to-pin electrical connections.
            world_size: Width and height of logical schematic world.
            size_function: Function returning symbol size.

        Returns:
            None.
        """
        indexed = {device.device_id: device for device in devices}
        targets: dict[str, list[tuple[str, float, float]]] = {}
        for left_id, left_pin, right_id, right_pin in connections:
            pairs = (
                (left_id, left_pin, right_id, right_pin),
                (right_id, right_pin, left_id, left_pin),
            )
            for passive_id, passive_pin, ic_id, ic_pin in pairs:
                passive = indexed[passive_id]
                integrated = indexed[ic_id]
                if (
                    passive.schematic_glued
                    or not cls._is_compact_passive(passive)
                    or len(passive.pins) != 2
                    or not integrated.reference.strip()
                    .casefold()
                    .startswith(("ic", "u"))
                ):
                    continue
                passive_terminal = cls._terminal_point(
                    passive, passive_pin, positions, rotations, size_function
                )
                ic_terminal = cls._terminal_point(
                    integrated, ic_pin, positions, rotations, size_function
                )
                passive_center = positions[passive_id]
                toward_ic = positions[ic_id] - passive_center
                terminal_offset = passive_terminal - passive_center
                if (
                    terminal_offset.x() * toward_ic.x()
                    + terminal_offset.y() * toward_ic.y()
                    <= 0.0
                ):
                    continue
                ic_offset = ic_terminal - positions[ic_id]
                if abs(ic_offset.x()) >= abs(ic_offset.y()):
                    targets.setdefault(passive_id, []).append(
                        (
                            "y",
                            ic_terminal.y() - terminal_offset.y(),
                            terminal_offset.x(),
                        )
                    )
                else:
                    targets.setdefault(passive_id, []).append(
                        (
                            "x",
                            ic_terminal.x() - terminal_offset.x(),
                            terminal_offset.y(),
                        )
                    )
        for device_id, values in targets.items():
            point = QPointF(positions[device_id])
            x_targets = [
                (value, priority) for axis, value, priority in values if axis == "x"
            ]
            y_targets = [
                (value, priority) for axis, value, priority in values if axis == "y"
            ]
            if x_targets:
                point.setX(max(x_targets, key=lambda item: item[1])[0])
            if y_targets:
                point.setY(max(y_targets, key=lambda item: item[1])[0])
            positions[device_id] = cls._clear_aligned_passive_overlap(
                indexed[device_id],
                point,
                "y" if y_targets else "x",
                devices,
                positions,
                rotations,
                world_size,
                size_function,
            )

    @classmethod
    def _clear_aligned_passive_overlap(
        cls,
        passive: Device,
        point: QPointF,
        aligned_axis: str,
        devices: list[Device],
        positions: dict[str, QPointF],
        rotations: dict[str, float],
        world_size: float,
        size_function: Callable[[Device], tuple[float, float]],
    ) -> QPointF:
        """Move an aligned passive perpendicular to its straight wire.

        Args:
            passive: Aligned resistor or capacitor.
            point: Candidate aligned center.
            aligned_axis: Coordinate fixed by terminal alignment.
            devices: All schematic components used as obstacles.
            positions: Current component centers.
            rotations: Current component rotations.
            world_size: Width and height of logical schematic world.
            size_function: Function returning symbol size.

        Returns:
            Non-overlapping center preserving the aligned coordinate.
        """
        width, height = cls._oriented_size(
            passive, rotations[passive.device_id], size_function
        )
        half_width = width / 2 + 4.0
        half_height = height / 2 + 4.0
        result = QPointF(point)
        direction = 0.0
        for _pass in range(len(devices) + 1):
            passive_bounds = QRectF(
                result.x() - half_width,
                result.y() - half_height,
                half_width * 2,
                half_height * 2,
            )
            collision = next(
                (
                    device
                    for device in devices
                    if device.device_id != passive.device_id
                    and cls._candidate_bounds(
                        device,
                        positions[device.device_id],
                        rotations,
                        size_function,
                    ).intersects(passive_bounds)
                ),
                None,
            )
            if collision is None:
                break
            obstacle = cls._candidate_bounds(
                collision,
                positions[collision.device_id],
                rotations,
                size_function,
            )
            if aligned_axis == "y":
                if direction == 0.0:
                    direction = (
                        -1.0
                        if result.x() <= positions[collision.device_id].x()
                        else 1.0
                    )
                if direction < 0.0:
                    result.setX(obstacle.left() - half_width - 8.0)
                else:
                    result.setX(obstacle.right() + half_width + 8.0)
            else:
                if direction == 0.0:
                    direction = (
                        -1.0
                        if result.y() <= positions[collision.device_id].y()
                        else 1.0
                    )
                if direction < 0.0:
                    result.setY(obstacle.top() - half_height - 8.0)
                else:
                    result.setY(obstacle.bottom() + half_height + 8.0)
        return QPointF(
            max(500.0, min(world_size - 500.0, result.x())),
            max(500.0, min(world_size - 500.0, result.y())),
        )

    @classmethod
    def _candidate_bounds(
        cls,
        device: Device,
        point: QPointF,
        rotations: dict[str, float],
        size_function: Callable[[Device], tuple[float, float]],
    ) -> QRectF:
        """Return optimizer collision bounds for one component.

        Args:
            device: Component whose bounds are required.
            point: Candidate component center.
            rotations: Current component rotations.
            size_function: Function returning symbol size.

        Returns:
            Padded candidate rectangle.
        """
        width, height = cls._oriented_size(
            device, rotations[device.device_id], size_function
        )
        padding = 4.0 if cls._is_compact_passive(device) else 12.0
        return QRectF(
            point.x() - width / 2 - padding,
            point.y() - height / 2 - padding,
            width + padding * 2,
            height + padding * 2,
        )

    @classmethod
    def _compact_translations(
        cls,
        devices: list[Device],
        positions: dict[str, QPointF],
        rotations: dict[str, float],
        edges: set[tuple[str, str]],
        world_size: float,
        size_function: Callable[[Device], tuple[float, float]],
        cancel_callback: Callable[[], bool],
    ) -> None:
        """Tighten layout using collision-free translations only.

        Args:
            devices: Components participating in final compaction.
            positions: Mutable component centers.
            rotations: Fixed component rotations.
            edges: Real electrical component edges.
            world_size: Width and height of logical schematic world.
            size_function: Function returning symbol size.
            cancel_callback: Callback returning whether optimization must stop.

        Returns:
            None.
        """
        indexed = {device.device_id: device for device in devices}
        movable = [device for device in devices if not device.schematic_glued]
        if len(movable) < 2:
            return
        cls._resolve_translation_overlaps(
            devices,
            positions,
            rotations,
            world_size,
            size_function,
            cancel_callback,
        )
        adjacency = {device.device_id: set() for device in devices}
        for left_id, right_id in edges:
            adjacency[left_id].add(right_id)
            adjacency[right_id].add(left_id)
        for _iteration in range(120):
            if cancel_callback():
                raise _OptimizationCancelled
            global_center = QPointF(
                sum(point.x() for point in positions.values()) / len(positions),
                sum(point.y() for point in positions.values()) / len(positions),
            )
            moved = False
            for device in sorted(movable, key=lambda item: item.reference.casefold()):
                device_id = device.device_id
                neighbors = adjacency[device_id]
                target = global_center
                if neighbors:
                    target = QPointF(
                        sum(positions[item].x() for item in neighbors) / len(neighbors),
                        sum(positions[item].y() for item in neighbors) / len(neighbors),
                    )
                for axis in ("x", "y"):
                    current = positions[device_id]
                    delta = (
                        target.x() - current.x()
                        if axis == "x"
                        else target.y() - current.y()
                    )
                    if abs(delta) < 1.0:
                        continue
                    step = math.copysign(min(20.0, abs(delta)), delta)
                    candidate = QPointF(current)
                    if axis == "x":
                        candidate.setX(current.x() + step)
                    else:
                        candidate.setY(current.y() + step)
                    candidate_bounds = cls._candidate_bounds(
                        device, candidate, rotations, size_function
                    )
                    if (
                        candidate_bounds.left() < 20.0
                        or candidate_bounds.top() < 20.0
                        or candidate_bounds.right() > world_size - 20.0
                        or candidate_bounds.bottom() > world_size - 20.0
                    ):
                        continue
                    if any(
                        cls._candidate_bounds(
                            other,
                            positions[other.device_id],
                            rotations,
                            size_function,
                        ).intersects(candidate_bounds)
                        for other in indexed.values()
                        if other.device_id != device_id
                    ):
                        continue
                    positions[device_id] = candidate
                    moved = True
            if not moved:
                break
        cls._resolve_translation_overlaps(
            devices,
            positions,
            rotations,
            world_size,
            size_function,
            cancel_callback,
        )

    @classmethod
    def _resolve_translation_overlaps(
        cls,
        devices: list[Device],
        positions: dict[str, QPointF],
        rotations: dict[str, float],
        world_size: float,
        size_function: Callable[[Device], tuple[float, float]],
        cancel_callback: Callable[[], bool],
    ) -> None:
        """Move overlapping free components to nearest clear grid point.

        Args:
            devices: Components participating in layout.
            positions: Mutable component centers.
            rotations: Fixed component rotations.
            world_size: Width and height of logical schematic world.
            size_function: Function returning symbol size.
            cancel_callback: Callback returning whether optimization must stop.

        Returns:
            None.
        """
        ordered = sorted(devices, key=lambda item: item.reference.casefold())
        for device in ordered:
            if device.schematic_glued:
                continue
            current = positions[device.device_id]
            current_bounds = cls._candidate_bounds(
                device, current, rotations, size_function
            )
            if not any(
                cls._candidate_bounds(
                    other,
                    positions[other.device_id],
                    rotations,
                    size_function,
                ).intersects(current_bounds)
                for other in ordered
                if other.device_id != device.device_id
            ):
                continue
            placed = False
            for ring in range(1, 31):
                if cancel_callback():
                    raise _OptimizationCancelled
                offsets = sorted(
                    {
                        (delta_x, delta_y)
                        for delta_x in range(-ring, ring + 1)
                        for delta_y in range(-ring, ring + 1)
                        if max(abs(delta_x), abs(delta_y)) == ring
                    },
                    key=lambda item: (
                        abs(item[0]) + abs(item[1]),
                        item[1],
                        item[0],
                    ),
                )
                for delta_x, delta_y in offsets:
                    candidate = current + QPointF(delta_x * 20.0, delta_y * 20.0)
                    candidate_bounds = cls._candidate_bounds(
                        device, candidate, rotations, size_function
                    )
                    if (
                        candidate_bounds.left() < 20.0
                        or candidate_bounds.top() < 20.0
                        or candidate_bounds.right() > world_size - 20.0
                        or candidate_bounds.bottom() > world_size - 20.0
                    ):
                        continue
                    if any(
                        cls._candidate_bounds(
                            other,
                            positions[other.device_id],
                            rotations,
                            size_function,
                        ).intersects(candidate_bounds)
                        for other in ordered
                        if other.device_id != device.device_id
                    ):
                        continue
                    positions[device.device_id] = candidate
                    placed = True
                    break
                if placed:
                    break

    @classmethod
    def _connection_segments(
        cls,
        connections: list[tuple[str, int, str, int]],
        devices: dict[str, Device],
        positions: dict[str, QPointF],
        rotations: dict[str, float],
        size_function: Callable[[Device], tuple[float, float]],
    ) -> list[tuple[tuple[str, str], tuple[QPointF, QPointF]]]:
        """Build pin-to-pin segments for the current candidate layout.

        Args:
            connections: Device and pin indexes sharing a NET.
            devices: Devices indexed by ID.
            positions: Candidate component centers.
            rotations: Candidate component rotations.
            size_function: Function returning each unrotated symbol size.

        Returns:
            Segments with their two owning device IDs.
        """
        segments = []
        for left_id, left_pin, right_id, right_pin in connections:
            left = cls._terminal_point(
                devices[left_id], left_pin, positions, rotations, size_function
            )
            right = cls._terminal_point(
                devices[right_id], right_pin, positions, rotations, size_function
            )
            segments.append(((left_id, right_id), (left, right)))
        return segments

    @classmethod
    def _segment_hits_rect(
        cls, segment: tuple[QPointF, QPointF], rectangle: QRectF
    ) -> bool:
        """Return whether a cable segment passes through a rectangle.

        Args:
            segment: Cable segment endpoints.
            rectangle: Candidate component obstacle rectangle.

        Returns:
            True when the segment enters or crosses the rectangle interior.
        """
        start, end = segment
        if rectangle.contains(
            QPointF((start.x() + end.x()) / 2, (start.y() + end.y()) / 2)
        ):
            return True
        corners = (
            QPointF(rectangle.left(), rectangle.top()),
            QPointF(rectangle.right(), rectangle.top()),
            QPointF(rectangle.right(), rectangle.bottom()),
            QPointF(rectangle.left(), rectangle.bottom()),
        )
        edges = tuple((corners[index], corners[(index + 1) % 4]) for index in range(4))
        return any(cls._segments_cross(segment, edge) for edge in edges)

    @classmethod
    def _score(
        cls,
        positions: dict[str, QPointF],
        rotations: dict[str, float],
        edges: set[tuple[str, str]],
        connections: list[tuple[str, int, str, int]],
        devices: dict[str, Device],
        size_function: Callable[[Device], tuple[float, float]],
    ) -> float:
        """Score crossings, wire length, and component overlap.

        Args:
            positions: Candidate center for each device ID.
            rotations: Candidate rotation for each device ID.
            edges: Shared-net graph edges.
            connections: Pin-to-pin connections grouped by NET.
            devices: Devices indexed by ID.
            size_function: Function returning each unrotated symbol size.

        Returns:
            Lower-is-better layout score.
        """
        connected = cls._connection_segments(
            connections, devices, positions, rotations, size_function
        )
        segments = [segment for _owners, segment in connected]
        if not segments:
            segments = [
                (positions[left], positions[right]) for left, right in sorted(edges)
            ]
        wire_lengths = [
            abs(right.x() - left.x()) + abs(right.y() - left.y())
            for left, right in segments
        ]
        score = sum(
            length * cls._WIRE_LENGTH_WEIGHT + length * length * cls._LONG_WIRE_WEIGHT
            for length in wire_lengths
        )
        score += sum(
            100_000.0
            for index, first in enumerate(segments)
            for second in segments[index + 1 :]
            if cls._segments_cross(first, second)
        )
        ordered = sorted(devices)
        bounds = {
            device_id: cls._candidate_bounds(
                devices[device_id],
                positions[device_id],
                rotations,
                size_function,
            )
            for device_id in ordered
        }
        for index, left in enumerate(ordered):
            score += sum(
                1_000_000_000.0
                for right in ordered[index + 1 :]
                if bounds[left].intersects(bounds[right])
            )
        connected_ids = {
            device_id for connection in connections for device_id in connection[::2]
        }
        if connected_ids:
            graph_center = QPointF(
                sum(positions[device_id].x() for device_id in connected_ids)
                / len(connected_ids),
                sum(positions[device_id].y() for device_id in connected_ids)
                / len(connected_ids),
            )
            for device_id in ordered:
                if device_id not in connected_ids:
                    distance = QLineF(positions[device_id], graph_center).length()
                    score += max(0.0, 6_000.0 - distance) * 20.0
            connected_positions = [positions[device_id] for device_id in connected_ids]
            span_width = max(point.x() for point in connected_positions) - min(
                point.x() for point in connected_positions
            )
            span_height = max(point.y() for point in connected_positions) - min(
                point.y() for point in connected_positions
            )
            score += (span_width + span_height) * cls._GRAPH_SPAN_WEIGHT
        for owners, segment in connected:
            for device_id in ordered:
                if device_id in owners:
                    continue
                if cls._segment_hits_rect(segment, bounds[device_id]):
                    score += 1_000_000_000.0
        return score

    @staticmethod
    def _layered_seed(
        devices: list[Device],
        positions: dict[str, QPointF],
        edges: set[tuple[str, str]],
    ) -> None:
        """Seed free devices in deterministic graph layers before local search.

        Args:
            devices: Components participating in the schematic.
            positions: Mutable component centers to initialize.
            edges: Undirected component graph edges.
        """
        adjacency: dict[str, set[str]] = {device.device_id: set() for device in devices}
        for left, right in edges:
            adjacency[left].add(right)
            adjacency[right].add(left)
        by_id = {device.device_id: device for device in devices}
        remaining = set(adjacency)
        components: list[set[str]] = []
        while remaining:
            seed = min(remaining)
            component = {seed}
            queue = [seed]
            remaining.remove(seed)
            while queue:
                current = queue.pop(0)
                for neighbor in sorted(adjacency[current]):
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        component.add(neighbor)
                        queue.append(neighbor)
            components.append(component)
        isolated = sorted(
            (
                next(iter(component))
                for component in components
                if len(component) == 1 and not adjacency[next(iter(component))]
            ),
            key=lambda device_id: by_id[device_id].reference.casefold(),
        )
        components = [
            component
            for component in components
            if not (len(component) == 1 and not adjacency[next(iter(component))])
        ]
        components.sort(key=lambda members: (-len(members), min(members)))
        origin = QPointF(
            sum(point.x() for point in positions.values()) / max(1, len(positions)),
            sum(point.y() for point in positions.values()) / max(1, len(positions)),
        )
        component_y = origin.y()
        for component in components:
            root = min(
                component,
                key=lambda device_id: (
                    -len(adjacency[device_id]),
                    by_id[device_id].reference.casefold(),
                ),
            )
            layers = {root: 0}
            queue = [root]
            while queue:
                current = queue.pop(0)
                for neighbor in sorted(
                    adjacency[current],
                    key=lambda device_id: by_id[device_id].reference.casefold(),
                ):
                    if neighbor in component and neighbor not in layers:
                        layers[neighbor] = layers[current] + 1
                        queue.append(neighbor)
            by_layer: dict[int, list[str]] = {}
            for device_id in component:
                by_layer.setdefault(layers.get(device_id, 0), []).append(device_id)
            layer_count = max(by_layer, default=0) + 1
            component_height = max(len(members) for members in by_layer.values()) * 260
            for layer, members in sorted(by_layer.items()):
                ordered = sorted(
                    members,
                    key=lambda device_id: by_id[device_id].reference.casefold(),
                )
                for index, device_id in enumerate(ordered):
                    if by_id[device_id].schematic_glued:
                        continue
                    positions[device_id] = QPointF(
                        origin.x() + (layer - (layer_count - 1) / 2) * 420,
                        component_y + (index - (len(ordered) - 1) / 2) * 260,
                    )
            component_y += component_height + 360
        columns = min(8, max(1, len(isolated)))
        for index, device_id in enumerate(isolated):
            if by_id[device_id].schematic_glued:
                continue
            positions[device_id] = QPointF(
                origin.x() + (index % columns - (columns - 1) / 2) * 260,
                component_y + (index // columns) * 220,
            )

    @classmethod
    def _spring_relax(
        cls,
        devices: list[Device],
        positions: dict[str, QPointF],
        rotations: dict[str, float],
        edges: set[tuple[str, str]],
        world_size: float,
        size_function: Callable[[Device], tuple[float, float]],
        cancel_callback: Callable[[], bool],
    ) -> None:
        """Pull connected devices together while repelling overlapping bodies.

        Args:
            devices: Components participating in the layout.
            positions: Mutable component centers.
            rotations: Current component rotations.
            edges: Visible electrical links between components.
            world_size: Width and height of the logical schematic world.
            size_function: Function returning each unrotated symbol size.
            cancel_callback: Callback returning whether optimization must stop.

        Returns:
            None.
        """
        by_id = {device.device_id: device for device in devices}
        movable = {device.device_id for device in devices if not device.schematic_glued}
        if not movable or not edges:
            return
        sibling_edges = cls._passive_sibling_edges(devices, edges)
        attraction_edges = edges | sibling_edges
        ordered = sorted(by_id)
        for _iteration in range(80):
            if cancel_callback():
                raise _OptimizationCancelled
            forces = {device_id: QPointF() for device_id in movable}
            for left_id, right_id in sorted(attraction_edges):
                delta = positions[right_id] - positions[left_id]
                distance = max(1.0, math.hypot(delta.x(), delta.y()))
                left_size = cls._oriented_size(
                    by_id[left_id], rotations[left_id], size_function
                )
                right_size = cls._oriented_size(
                    by_id[right_id], rotations[right_id], size_function
                )
                if (left_id, right_id) in sibling_edges:
                    target = (
                        cls._spring_minimum_spacing(
                            by_id[left_id],
                            by_id[right_id],
                            left_size,
                            right_size,
                            delta,
                            distance,
                        )
                        + 12.0
                    )
                else:
                    target = cls._spring_target_spacing(
                        by_id[left_id], by_id[right_id], left_size, right_size
                    )
                pull = max(0.0, min(90.0, (distance - target) * 0.08))
                direction = QPointF(delta.x() / distance, delta.y() / distance)
                if left_id in forces:
                    forces[left_id] += direction * pull
                if right_id in forces:
                    forces[right_id] -= direction * pull
            for left_index, left_id in enumerate(ordered):
                for right_id in ordered[left_index + 1 :]:
                    delta = positions[right_id] - positions[left_id]
                    distance = max(1.0, math.hypot(delta.x(), delta.y()))
                    left_size = cls._oriented_size(
                        by_id[left_id], rotations[left_id], size_function
                    )
                    right_size = cls._oriented_size(
                        by_id[right_id], rotations[right_id], size_function
                    )
                    minimum = cls._spring_minimum_spacing(
                        by_id[left_id],
                        by_id[right_id],
                        left_size,
                        right_size,
                        delta,
                        distance,
                    )
                    if distance >= minimum:
                        continue
                    push = min(100.0, (minimum - distance) * 0.22)
                    direction = QPointF(delta.x() / distance, delta.y() / distance)
                    if left_id in forces:
                        forces[left_id] -= direction * push
                    if right_id in forces:
                        forces[right_id] += direction * push
            maximum_step = 90.0
            for device_id, force in forces.items():
                magnitude = max(1.0, math.hypot(force.x(), force.y()))
                if magnitude > maximum_step:
                    force *= maximum_step / magnitude
                point = positions[device_id] + force
                positions[device_id] = QPointF(
                    max(500.0, min(world_size - 500.0, point.x())),
                    max(500.0, min(world_size - 500.0, point.y())),
                )
        for device_id in movable:
            point = positions[device_id]
            positions[device_id] = QPointF(
                round(point.x() / 20.0) * 20.0,
                round(point.y() / 20.0) * 20.0,
            )

    @classmethod
    def optimize(
        cls,
        devices: list[Device],
        positions: dict[str, QPointF],
        rotations: dict[str, float],
        edges: set[tuple[str, str]],
        connections: list[tuple[str, int, str, int]],
        world_size: float,
        size_function: Callable[[Device], tuple[float, float]],
        progress_callback: Callable[[int], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, QPointF], dict[str, float]]:
        """Optimize unglued devices on a bounded deterministic search grid.

        Args:
            devices: Devices participating in the schematic.
            positions: Initial center positions indexed by device ID.
            rotations: Initial rotations indexed by device ID.
            edges: Shared-net graph edges.
            connections: Pin-to-pin connections grouped by NET.
            world_size: Width and height of the logical schematic world.
            size_function: Function returning each unrotated symbol size.
            progress_callback: Optional callback receiving progress percentages.
            cancel_callback: Optional callback returning whether optimization must stop.

        Returns:
            Optimized position and rotation mappings.
        """
        indexed = {device.device_id: device for device in devices}
        symbol_sizes = {device.device_id: size_function(device) for device in devices}

        def cached_size(device: Device) -> tuple[float, float]:
            """Return precomputed symbol size for repeated score calls."""
            return symbol_sizes[device.device_id]

        should_cancel = cancel_callback or (lambda: False)
        connected_ids = {device_id for edge in edges for device_id in edge} | {
            device_id for connection in connections for device_id in connection[::2]
        }
        scored_ids = connected_ids | {
            device.device_id for device in devices if device.schematic_glued
        }
        scored = {
            device_id: device
            for device_id, device in indexed.items()
            if device_id in scored_ids
        }
        movable = [
            device
            for device in devices
            if not device.schematic_glued and device.device_id in connected_ids
        ]
        cls._layered_seed(devices, positions, edges)
        cls._spring_relax(
            devices,
            positions,
            rotations,
            edges,
            world_size,
            cached_size,
            should_cancel,
        )
        cls._align_passive_terminals(
            devices,
            positions,
            rotations,
            connections,
            world_size,
            cached_size,
        )
        total = max(1, len(movable) * cls._LOCAL_PASSES)
        completed = 0
        if not movable and progress_callback is not None:
            progress_callback(100)
        for _pass in range(cls._LOCAL_PASSES):
            changed = False
            for device in sorted(movable, key=lambda item: item.reference.casefold()):
                if should_cancel():
                    raise _OptimizationCancelled
                current = positions[device.device_id]
                current_rotation = rotations[device.device_id]
                best = (
                    cls._score(
                        positions,
                        rotations,
                        edges,
                        connections,
                        scored,
                        cached_size,
                    ),
                    current,
                    current_rotation,
                )
                for dx, dy in cls._LOCAL_OFFSETS:
                    if should_cancel():
                        raise _OptimizationCancelled
                    candidate = QPointF(
                        max(500.0, min(world_size - 500.0, current.x() + dx)),
                        max(500.0, min(world_size - 500.0, current.y() + dy)),
                    )
                    for rotation in cls._candidate_rotations(device, current_rotation):
                        positions[device.device_id] = candidate
                        rotations[device.device_id] = rotation
                        score = cls._score(
                            positions,
                            rotations,
                            edges,
                            connections,
                            scored,
                            cached_size,
                        )
                        if score < best[0]:
                            best = (score, candidate, rotation)
                positions[device.device_id] = best[1]
                rotations[device.device_id] = best[2]
                changed = changed or best[1] != current or best[2] != current_rotation
                completed += 1
                if progress_callback is not None:
                    progress_callback(round(completed * 100 / total))
            if not changed:
                break
        cls._align_passive_terminals(
            devices,
            positions,
            rotations,
            connections,
            world_size,
            cached_size,
        )
        cls._compact_translations(
            devices,
            positions,
            rotations,
            edges,
            world_size,
            cached_size,
            should_cancel,
        )
        cls._align_passive_terminals(
            devices,
            positions,
            rotations,
            connections,
            world_size,
            cached_size,
        )
        cls._resolve_translation_overlaps(
            devices,
            positions,
            rotations,
            world_size,
            cached_size,
            should_cancel,
        )
        if progress_callback is not None:
            progress_callback(100)
        return positions, rotations


class SchematicOptimizationWorker(QThread):
    """Run layout optimization away from the Qt GUI thread."""

    progress = Signal(int)
    completed = Signal(object, object)
    cancelled = Signal()

    def __init__(
        self,
        devices: list[Device],
        positions: dict[str, QPointF],
        rotations: dict[str, float],
        edges: set[tuple[str, str]],
        connections: list[tuple[str, int, str, int]],
        world_size: float,
        size_function: Callable[[Device], tuple[float, float]],
    ) -> None:
        """Store an immutable optimization request.

        Args:
            devices: Devices participating in the schematic.
            positions: Initial centers indexed by device ID.
            rotations: Initial rotations indexed by device ID.
            edges: Shared-net graph edges.
            connections: Pin-to-pin connections grouped by NET.
            world_size: Width and height of the logical schematic world.
            size_function: Function returning each unrotated symbol size.
        """
        super().__init__()
        self._devices = devices
        self._positions = positions
        self._rotations = rotations
        self._edges = edges
        self._connections = connections
        self._world_size = world_size
        self._size_function = size_function

    def run(self) -> None:
        """Optimize the snapshot and emit progress and the final mappings."""
        try:
            positions, rotations = SchematicLayoutOptimizer.optimize(
                self._devices,
                self._positions,
                self._rotations,
                self._edges,
                self._connections,
                self._world_size,
                self._size_function,
                self.progress.emit,
                self.isInterruptionRequested,
            )
        except _OptimizationCancelled:
            self.cancelled.emit()
            return
        self.completed.emit(positions, rotations)
