"""Dual-face and single-face board-footprint movement."""

from __future__ import annotations

# PySide6 exposes Qt classes through compiled extension modules; Pylint cannot
# inspect those names despite them being available at runtime.
# pylint: disable=no-name-in-module,too-few-public-methods
# pylint: disable=too-many-arguments,too-many-positional-arguments

from dataclasses import replace
import math

from PySide6.QtWidgets import QMessageBox

from ..project import Device, Pad
from .app_support import LOGGER


class MoveActionsMixin:
    """Move a footprint through one of its physical board pads."""

    def _cycle_move_mode(self) -> None:
        """Enter or alternate the dual-face and single-face footprint move modes."""
        if not self.project:
            if hasattr(self, "_move_button"):
                self._move_button.setChecked(False)
            QMessageBox.information(
                self, "No project", "Create or open a project first."
            )
            return
        mode = "single" if self._move_mode == "dual" else "dual"
        self._set_move_mode(mode)

    def _set_move_mode(self, mode: str | None) -> None:
        """Set footprint movement scope, or leave movement mode.

        ``dual`` moves the selected footprint on both mirrored board faces.
        ``single`` moves only the clicked face, preserving that object's
        per-face offset in its independently persisted pad coordinates.
        """
        if mode not in {None, "dual", "single"}:
            raise ValueError("move mode must be dual, single, or None")
        self._commit_pad_move()
        for view in (*self._views.values(), *self._side_views.values()):
            view.set_move_mode(False)
        self._move_mode = mode
        button = getattr(self, "_move_button", None)
        if button is not None:
            button.blockSignals(True)
            button.setChecked(mode is not None)
            button.blockSignals(False)
        if mode is None:
            self.statusBar().clearMessage()
            return
        self._exit_connection_mode()
        self._set_nc_mode(False)
        self._set_delete_mode(False)
        self._disable_ruler()
        if self._pending_pad is not None:
            self._cancel_pad_placement()
        if self._pending_device is not None:
            self._cancel_device_placement()
        for view in (*self._views.values(), *self._side_views.values()):
            view.set_move_mode(True)
        self._show_move_status()

    def _show_move_status(self) -> None:
        """Keep the active movement scope visible in the status bar."""
        if self._move_mode is not None:
            self.statusBar().showMessage(
                "Dual face move" if self._move_mode == "dual" else "Single face move"
            )

    def _start_pad_move(self, side: str, pad_id: str, x: float, y: float) -> None:
        """Select a footprint through one Shift-clicked pad as its move handle."""
        if self._move_mode is None or not self.project:
            return
        self._commit_pad_move()
        pad = next(
            (
                candidate
                for candidate in self.project.pads
                if candidate.pad_id == pad_id and candidate.side == side
            ),
            None,
        )
        if pad is None:
            return
        if pad.device_id is None:
            targets = (pad,)
            device = None
        else:
            targets = tuple(
                candidate
                for candidate in self.project.pads
                if candidate.device_id == pad.device_id
                and (self._move_mode == "dual" or candidate.side == side)
            )
            device = next(
                (
                    candidate
                    for candidate in self.project.devices
                    if candidate.device_id == pad.device_id
                ),
                None,
            )
        self._moving_pad_context = {
            "side": side,
            "pad_id": pad_id,
            "start": (x, y),
            "pads": targets,
            "device": device,
            "move_device": device is not None
            and any(target.side == device.side for target in targets),
            "changed": False,
            "was_dirty": self._dirty,
        }
        LOGGER.info(
            "Footprint move selected pad=%s side=%s mode=%s pads=%s",
            pad_id,
            side,
            self._move_mode,
            len(targets),
        )
        self._show_move_status()

    @staticmethod
    def _bounded_pad_move_delta(
        side: str,
        pads: tuple[Pad, ...],
        device: Device | None,
        move_device: bool,
        delta_x: float,
        delta_y: float,
    ) -> tuple[float, float]:
        """Clamp one mirrored movement so every pad remains inside its image."""
        minimum_x = -math.inf
        maximum_x = math.inf
        minimum_y = -math.inf
        maximum_y = math.inf
        for pad in pads:
            sign = 1.0 if pad.side == side else -1.0
            if sign > 0:
                minimum_x = max(minimum_x, -pad.x)
                maximum_x = min(maximum_x, 1.0 - pad.width - pad.x)
            else:
                minimum_x = max(minimum_x, pad.x - (1.0 - pad.width))
                maximum_x = min(maximum_x, pad.x)
            minimum_y = max(minimum_y, -pad.y)
            maximum_y = min(maximum_y, 1.0 - pad.height - pad.y)
        if move_device and device is not None:
            sign = 1.0 if device.side == side else -1.0
            if sign > 0:
                minimum_x = max(minimum_x, -device.x)
                maximum_x = min(maximum_x, 1.0 - device.x)
            else:
                minimum_x = max(minimum_x, device.x - 1.0)
                maximum_x = min(maximum_x, device.x)
            minimum_y = max(minimum_y, -device.y)
            maximum_y = min(maximum_y, 1.0 - device.y)
        return (
            max(minimum_x, min(delta_x, maximum_x)),
            max(minimum_y, min(delta_y, maximum_y)),
        )

    def _update_pad_move(self, side: str, pad_id: str, x: float, y: float) -> None:
        """Move the selected footprint live without rebuilding image viewports."""
        context = self._moving_pad_context
        if (
            context is None
            or not self.project
            or context["side"] != side
            or context["pad_id"] != pad_id
        ):
            return
        start_x, start_y = context["start"]
        delta_x, delta_y = self._bounded_pad_move_delta(
            side,
            context["pads"],
            context["device"],
            context["move_device"],
            x - start_x,
            y - start_y,
        )
        moved_pads = {
            pad.pad_id: replace(
                pad,
                x=pad.x + (delta_x if pad.side == side else -delta_x),
                y=pad.y + delta_y,
            )
            for pad in context["pads"]
        }
        self.project.pads = [
            moved_pads.get(pad.pad_id, pad) for pad in self.project.pads
        ]
        device = context["device"]
        if context["move_device"] and device is not None:
            moved_device = replace(
                device,
                x=device.x + (delta_x if device.side == side else -delta_x),
                y=device.y + delta_y,
            )
            self.project.devices = [
                moved_device if item.device_id == device.device_id else item
                for item in self.project.devices
            ]
        changed = not (
            math.isclose(delta_x, 0.0, abs_tol=1e-9)
            and math.isclose(delta_y, 0.0, abs_tol=1e-9)
        )
        context["changed"] = changed
        self._dirty = context["was_dirty"] or changed
        self._refresh_moved_footprint({pad.side for pad in context["pads"]})

    def _finish_pad_move(self, side: str, pad_id: str, x: float, y: float) -> None:
        """Place the live footprint at its final pointer position."""
        self._update_pad_move(side, pad_id, x, y)
        self._commit_pad_move()

    def _commit_pad_move(self) -> None:
        """Finish an active move and record exactly one undo history state."""
        context = getattr(self, "_moving_pad_context", None)
        if context is None:
            return
        self._moving_pad_context = None
        if context["changed"]:
            self._dirty = True
            self._update_title()
            LOGGER.info(
                "Footprint move committed pad=%s mode=%s",
                context["pad_id"],
                self._move_mode,
            )
        else:
            self._dirty = context["was_dirty"]
        self._show_move_status()

    def _refresh_moved_footprint(self, sides: set[str]) -> None:
        """Redraw moved pads and footprints without touching zoom or pan."""
        for side in sides:
            labels = self._display_labels_for_side(side)
            footprints = self._vector_footprints_for_side(side)
            for view in (self._views[side], self._side_views[side]):
                view.set_pad_labels(labels)
                view.set_footprint_overlays(footprints)
        self._overlay_view.set_pad_labels(self._overlay_pad_labels())
