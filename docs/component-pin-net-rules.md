# Component, pad, pin, and net rules

## Purpose

Tnasrevner distinguishes independent board pads from KiCad components. A pad is a
physical contact recorded on the board image. A KiCad component is an object placed
from a footprint source and owns logical pins.

## Object model

```text
Independent pad P1
  └── net_id: GND

KiCad component U1
  └── component pin 1
       ├── footprint_pad: 1
       ├── pin_id: GND
       ├── function: Ground
       └── net_id: GND
```

The persisted relationship is:

```text
U1.1 → ComponentPin(pin_id=GND, function=Ground) → Net:GND
P1   → Net:GND
```

## Rules

- `Pad` represents an independent physical pad or connector contact.
- `Device` represents a placed KiCad component and footprint. It is the current
  persistence name; future UI/documentation may call it `KiCadComponent`.
- `ComponentPin.number` is the KiCad logical pin number.
- `ComponentPin.footprint_pad` maps the logical pin to the physical footprint pad.
- `ComponentPin.pin_id` is the user-defined stable identity, such as `GND`.
- `ComponentPin.function` is the human-readable role, such as `Ground` or `VCC`.
- `ComponentPin.net_id` links the pin to a logical net.
- A generated footprint pad keeps its physical `Pad` record for placement and
  geometry; its component pin stores the electrical meaning.
- Connecting a generated pad to a net synchronizes the associated component pin.
- Old projects without a `pins` field remain readable; pins are added as components
  are edited or newly placed.

## Editing workflow

1. Place a KiCad component from **Add Footprint**.
2. Right-click one of its generated pads, such as `U1.1`.
3. Select **Edit pin ID/function**.
4. Set `pin_id` to `GND` and `function` to `Ground`.
5. Select **Connect to net** and set `net_id` to `GND`.

The schematic model consumes component pins and independent pads through their shared
net IDs. Physical image coordinates remain separate from electrical connectivity.

## Out of scope for this rule

These rules do not yet define symbol-library selection, automatic symbol pin mapping,
electrical pin types, netlist export, or final schematic layout. Those belong to the
Schematic milestone.
