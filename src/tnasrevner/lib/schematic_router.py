"""Orthogonal Manhattan routing for schematic net segments."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,too-many-locals,too-few-public-methods
# pylint: disable=unbalanced-tuple-unpacking

from heapq import heappop, heappush
import math

from PySide6.QtCore import QPointF, QRectF


class OrthogonalRouter:
    """Route schematic wires on a Manhattan grid around obstacles."""

    GRID = 40.0
    BEND_COST = 30.0
    OBSTACLE_COST = 10_000.0
    WIRE_CROSSING_COST = 500.0
    PROXIMITY_COST = 50.0

    @classmethod
    def route(
        cls,
        start: QPointF,
        end: QPointF,
        obstacles: tuple[QRectF, ...] = (),
        existing: tuple[tuple[QPointF, QPointF], ...] = (),
    ) -> list[QPointF]:
        """Find an orthogonal path between two terminals.

        Args:
            start: Exact route origin.
            end: Exact route destination.
            obstacles: Component rectangles to avoid.
            existing: Previously routed segments used for congestion cost.

        Returns:
            Simplified orthogonal polyline including exact endpoints.
        """
        if start == end:
            return [start, end]
        direct = cls._direct_route(start, end, obstacles, existing)
        if direct is not None:
            return direct
        search_area = QRectF(
            min(start.x(), end.x()) - 240,
            min(start.y(), end.y()) - 240,
            abs(end.x() - start.x()) + 480,
            abs(end.y() - start.y()) + 480,
        )
        nearby_obstacles = tuple(
            obstacle
            for obstacle in obstacles
            if obstacle.adjusted(-240, -240, 240, 240).intersects(search_area)
        )
        minimum_x = search_area.left()
        maximum_x = search_area.right()
        minimum_y = search_area.top()
        maximum_y = search_area.bottom()
        origin = (round(start.x() / cls.GRID), round(start.y() / cls.GRID))
        target = (round(end.x() / cls.GRID), round(end.y() / cls.GRID))
        queue: list[tuple[float, float, tuple[int, int], int | None]] = []
        heappush(queue, (0.0, 0.0, origin, None))
        costs = {(origin, None): 0.0}
        parents: dict[tuple[tuple[int, int], int | None], object] = {}
        directions = ((1, 0), (0, 1), (-1, 0), (0, -1))
        found: tuple[tuple[int, int], int | None] | None = None
        while queue:
            _priority, current_cost, point, previous_direction = heappop(queue)
            if point == target:
                found = (point, previous_direction)
                break
            for direction, (delta_x, delta_y) in enumerate(directions):
                next_point = point[0] + delta_x, point[1] + delta_y
                logical = QPointF(next_point[0] * cls.GRID, next_point[1] * cls.GRID)
                if not (
                    minimum_x <= logical.x() <= maximum_x
                    and minimum_y <= logical.y() <= maximum_y
                ):
                    continue
                if cls._inside_obstacle(
                    logical, nearby_obstacles
                ) and next_point not in (
                    origin,
                    target,
                ):
                    continue
                segment = (
                    QPointF(point[0] * cls.GRID, point[1] * cls.GRID),
                    logical,
                )
                cost = current_cost + cls.GRID
                if previous_direction is not None and previous_direction != direction:
                    cost += cls.BEND_COST
                if cls._near_existing(segment, existing):
                    cost += cls.WIRE_CROSSING_COST
                key = (next_point, direction)
                if cost >= costs.get(key, math.inf):
                    continue
                costs[key] = cost
                parents[key] = (point, previous_direction)
                heuristic = abs(target[0] - next_point[0]) + abs(
                    target[1] - next_point[1]
                )
                heappush(
                    queue, (cost + heuristic * cls.GRID, cost, next_point, direction)
                )
        if found is None:
            return [start, QPointF(end.x(), start.y()), end]
        points = []
        state = found
        while state in parents:
            points.append(QPointF(state[0][0] * cls.GRID, state[0][1] * cls.GRID))
            state = parents[state]
        points.append(QPointF(origin[0] * cls.GRID, origin[1] * cls.GRID))
        points.reverse()
        return cls._attach_exact_endpoints(start, end, points)

    @classmethod
    def _direct_route(
        cls,
        start: QPointF,
        end: QPointF,
        obstacles: tuple[QRectF, ...],
        existing: tuple[tuple[QPointF, QPointF], ...],
    ) -> list[QPointF] | None:
        """Return shortest clear L route before running grid search.

        Args:
            start: Exact route origin.
            end: Exact route destination.
            obstacles: Component rectangles to avoid.
            existing: Previously routed segments to avoid overlapping.

        Returns:
            Clear orthogonal route, or ``None`` when both L routes are blocked.
        """
        candidates = (
            cls.simplify([start, QPointF(end.x(), start.y()), end]),
            cls.simplify([start, QPointF(start.x(), end.y()), end]),
        )
        clear = [
            path
            for path in candidates
            if all(
                cls._axis_segment_clear(left, right, obstacles)
                for left, right in zip(path, path[1:])
            )
        ]
        if not clear:
            return None
        return min(
            clear,
            key=lambda path: (
                sum(
                    cls._segments_overlap((left, right), other)
                    for left, right in zip(path, path[1:])
                    for other in existing
                ),
                abs(path[1].x() - path[0].x()) + abs(path[1].y() - path[0].y()),
                tuple((point.x(), point.y()) for point in path),
            ),
        )

    @staticmethod
    def _segments_overlap(
        first: tuple[QPointF, QPointF], second: tuple[QPointF, QPointF]
    ) -> bool:
        """Return whether collinear orthogonal segments overlap in length.

        Args:
            first: First orthogonal segment.
            second: Second orthogonal segment.

        Returns:
            True when segments share more than one endpoint.
        """
        first_start, first_end = first
        second_start, second_end = second
        if first_start.x() == first_end.x() == second_start.x() == second_end.x():
            return max(
                min(first_start.y(), first_end.y()),
                min(second_start.y(), second_end.y()),
            ) < min(
                max(first_start.y(), first_end.y()),
                max(second_start.y(), second_end.y()),
            )
        if first_start.y() == first_end.y() == second_start.y() == second_end.y():
            return max(
                min(first_start.x(), first_end.x()),
                min(second_start.x(), second_end.x()),
            ) < min(
                max(first_start.x(), first_end.x()),
                max(second_start.x(), second_end.x()),
            )
        return False

    @staticmethod
    def _axis_segment_clear(
        start: QPointF, end: QPointF, obstacles: tuple[QRectF, ...]
    ) -> bool:
        """Return whether one orthogonal segment avoids obstacle interiors.

        Args:
            start: Segment origin.
            end: Segment destination.
            obstacles: Component rectangles to avoid.

        Returns:
            True when segment does not enter an obstacle interior.
        """
        if start.y() == end.y():
            left, right = sorted((start.x(), end.x()))
            return not any(
                obstacle.top() < start.y() < obstacle.bottom()
                and max(left, obstacle.left()) < min(right, obstacle.right())
                for obstacle in obstacles
            )
        top, bottom = sorted((start.y(), end.y()))
        return not any(
            obstacle.left() < start.x() < obstacle.right()
            and max(top, obstacle.top()) < min(bottom, obstacle.bottom())
            for obstacle in obstacles
        )

    @classmethod
    def _attach_exact_endpoints(
        cls, start: QPointF, end: QPointF, points: list[QPointF]
    ) -> list[QPointF]:
        """Join off-grid terminals to grid path using only right angles.

        Args:
            start: Exact route origin.
            end: Exact route destination.
            points: Grid-aligned route points.

        Returns:
            Simplified route containing exact endpoints.
        """
        first_grid = points[0]
        last_grid = points[-1]
        result = [start]
        if start.x() != first_grid.x() and start.y() != first_grid.y():
            first_direction_is_horizontal = (
                len(points) > 1 and points[0].y() == points[1].y()
            )
            result.append(
                QPointF(start.x(), first_grid.y())
                if first_direction_is_horizontal
                else QPointF(first_grid.x(), start.y())
            )
        result.extend(points)
        if last_grid.x() != end.x() and last_grid.y() != end.y():
            last_direction_is_horizontal = (
                len(points) > 1 and points[-2].y() == points[-1].y()
            )
            result.append(
                QPointF(end.x(), last_grid.y())
                if last_direction_is_horizontal
                else QPointF(last_grid.x(), end.y())
            )
        result.append(end)
        return cls.simplify(result)

    @staticmethod
    def _inside_obstacle(point: QPointF, obstacles: tuple[QRectF, ...]) -> bool:
        """Return whether a grid point lies inside an obstacle."""
        return any(rect.contains(point) for rect in obstacles)

    @staticmethod
    def _near_existing(
        segment: tuple[QPointF, QPointF],
        existing: tuple[tuple[QPointF, QPointF], ...],
    ) -> bool:
        """Return whether a candidate segment is adjacent to a routed wire."""
        return any(
            OrthogonalRouter._segments_overlap(segment, other) for other in existing
        )

    @staticmethod
    def simplify(points: list[QPointF]) -> list[QPointF]:
        """Remove duplicate and collinear points from a polyline."""
        result: list[QPointF] = []
        for point in points:
            if result and point == result[-1]:
                continue
            if len(result) >= 2:
                previous, current = result[-2:]
                if (previous.x() == current.x() == point.x()) or (
                    previous.y() == current.y() == point.y()
                ):
                    result[-1] = point
                    continue
            result.append(point)
        return result
