# Keyboard and mouse shortcuts

This page lists the controls implemented by Tna’’Srevn’Er. Mouse actions are
included because most editing modes combine a modifier key with a click or drag.

On macOS, Qt displays the platform equivalent of `Ctrl` as `Command` in menus.
`Meta` below means the Command key on macOS (and the operating-system Meta key
on other platforms).

## Common to all application modes

| Input | Action |
| --- | --- |
| `Ctrl+N` | Create a new project. |
| `Ctrl+O` | Open a project. |
| `Ctrl+S` | Save the current project. |
| `Ctrl+Shift+S` | Save the current project under another name. |
| `Ctrl+W` | Close the current project. |
| `I` | Open the Top/Bottom image manager. |
| `T` | Show the Top board view. |
| `B` | Show the Bottom board view. |
| Press `T` twice or `B` twice within 0.4 seconds | Show the dual Top + Bottom view. |
| `Esc` | Leave the active temporary mode: Delete, Measure, Add Pad, Connect, or Add Component. |

## Board navigation and selection

| Input | Action |
| --- | --- |
| Mouse wheel or trackpad pinch | Zoom the board image around the view center. |
| Left-drag empty image space | Pan the board image. |
| Left-click a pad | Select the pad and highlight its existing net. |
| Right-click a pad, component, or connection | Open its context menu. |
| `Shift` + right-click a pad/component | Open the pad or component editing menu. |

Top and Bottom use one synchronized viewport in the dual view. Selecting,
creating, connecting, or deleting an object must not change its zoom or pan.

## Connect mode

Activate **Connect** in the Tools palette.

| Input | Action |
| --- | --- |
| Hold `Shift` and left-click pads or schematic pins | Add terminals to a rolling connection. Each new terminal links to the previous one. |
| Release `Shift` | Finish the current rolling selection and remove its preview; Connect mode remains active. |
| Plain left-click a board pad | Inspect/highlight its existing net without adding a link. |
| Right-click a displayed board connection | Open the **Disconnect** action. |
| `Esc` | Leave Connect mode. |

Pressing or releasing `Shift` only changes the connection preview. It never
changes board zoom or pan.

## Not-connected (NC) mode

Activate **Set Pin not connected** (`⛓️‍💥`) in the Tools palette.

| Input | Action |
| --- | --- |
| `Shift` + left-click a board pad or schematic pin | Assign the reserved `NC` state to that terminal. |
| Click the NC tool again | Leave NC mode. |

## Delete mode

Activate **Delete Component** in the Tools palette.

| Input | Action |
| --- | --- |
| Left-click a pad or component | Delete it. Deleting a component also deletes its generated pads. |
| Continue left-clicking | Delete more objects without leaving the mode. |
| `Esc` | Leave Delete mode. |

Deletion never changes the active tab, zoom, or pan.

## Measure mode

Activate **Measure Tool** (`📐`) in the Tools palette. The image must have a
saved physical calibration.

| Input | Action |
| --- | --- |
| First left-click | Set the measurement start point. |
| Second left-click | Set the end point and display the distance in millimetres. |
| `Esc` | Leave Measure mode and clear the temporary ruler. |

## Add Pad mode

Activate **Add a Pad** in the Tools palette.

| Input | Action |
| --- | --- |
| Left-drag | Draw and create the rectangular pad. The mode remains active for the next pad. |
| `Meta` + left-drag | Temporarily pan without placing a pad. |
| `Esc` | Stop continuous pad placement. |

Creating a pad never changes the active tab, zoom, or pan.

## Add Component mode

Activate **Add Component**, choose a KiCad footprint, and enter its reference.

| Input | Action |
| --- | --- |
| Move the pointer | Position the footprint preview. |
| Left-click | Place the component and arm the next available reference. |
| Right-click | Rotate the pending component clockwise by 45 degrees. |
| `Meta` + left-drag | Temporarily pan without placing the component. |
| `Esc` | End the component-placement series. |

Component placement never changes the active tab, zoom, or pan.

## Schematic view

| Input | Action |
| --- | --- |
| Mouse wheel or trackpad pinch | Zoom around the pointer. |
| Left-drag empty canvas space | Pan the schematic. |
| Left-drag an unglued component or independent pad | Move it. |
| Left-click a terminal | Select it and highlight its net. |
| Right-click a terminal or component | Open its context menu. |
| `Shift` + right-click a terminal | Edit its net name directly. |
| Connect mode + `Shift` + left-click terminals | Create rolling links. |

## Image editor

These controls apply while importing, aligning, scaling, or cropping a Top or
Bottom board photograph.

### Common image-editor controls

| Input | Action |
| --- | --- |
| Mouse wheel or trackpad pinch | Zoom the image. |
| `Esc` | Cancel and close the editor without applying its changes. |

### Scale line mode

| Input | Action |
| --- | --- |
| Left-drag from one point to another | Draw the physical reference line; enter its real length in millimetres. |

### Align line mode

| Input | Action |
| --- | --- |
| Left-drag along an edge | Rotate the image so that the drawn edge becomes horizontal. |

### Scale footprint mode

| Input | Action |
| --- | --- |
| Left-drag inside the footprint | Move the calibration footprint. |
| Left-drag its lower-right scale handle | Resize the footprint to match the photograph. |
| Right-click | Rotate the calibration footprint clockwise by 45 degrees. |

### Crop rectangle mode

| Input | Action |
| --- | --- |
| Left-drag without `Shift` | Pan the image. |
| `Shift` + left-drag | Draw a new crop rectangle. |
| `Shift` + left-drag a crop edge or corner | Resize the crop rectangle. |

The crop rectangle follows image rotation and remains attached to the same
source-image area.
