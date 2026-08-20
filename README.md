<p align="center">
  <img src="ressources/logosmall.png" alt="Tna’’Srevn’Er logo" width="280">
</p>

# Tna’’Srevn’Er

Tna’’Srevn’Er is a beta cross-platform Qt application for reverse-engineering
PCBs. It runs on Linux and macOS.

The goal is to recreate a board’s logical point-to-point connections without
having to painstakingly follow every physical copper trace. Import board
images, place components and pads, assign nets, and inspect the resulting
connections. From those connections, Tna’’Srevn’Er automatically generates an
electrical schematic.

## Main workflow

- Import, align, and scale the top and bottom board images using a component
  footprint as a reference.
- Place components, footprints, pads, and pins, including through-hole devices.
- Recreate point-to-point connections; Tna’’Srevn’Er generates and organizes
  the corresponding net connections.
- Inspect the board through the schematic, connections, net, and BOM views.
- Automatically generate the electrical schematic from the defined links.

## Screenshots

<p align="center">
  <img src="ressources/Screenshot%202026-08-14%20at%2008.26.01.png" alt="Top board view" width="48%"><br>
  <em>Top-side board view with the imported PCB image, components, pads, and connections.</em>
</p>
<p align="center">
  <img src="ressources/Screenshot%202026-08-20%20at%2018.06.49.png" alt="Both board faces" width="48%"><br>
  <em>Combined view showing the two board faces and their aligned elements.</em>
</p>
<p align="center">
  <img src="ressources/Screenshot%202026-08-20%20at%2018.06.41.png" alt="Automatically generated schematic" width="48%"><br>
  <em>Electrical schematic generated automatically from the defined point-to-point links.</em>
</p>
<p align="center">
  <img src="ressources/Screenshot%202026-08-14%20at%2008.26.45.png" alt="Connections table" width="48%"><br>
  <em>Connections view used to inspect and organize the generated net connections.</em>
</p>

### Adding a footprint from the KiCad library

<p align="center">
  <img src="ressources/Screenshot%202026-08-04%20at%2008.39.15.png" alt="Adding a footprint from the KiCad library" width="70%"><br>
  <em>Adding a footprint directly from the KiCad library.</em>
</p>

## Feedback welcome

This project is currently in beta. Please try it out and do not hesitate to
report bugs, usability issues, or missing features through the issue tracker.
Pull requests and improvements are welcome too.

## Greetings and third-party data

Special thanks to the KiCad project and its contributors for maintaining the
libraries that help identify components and map their pins.

KiCad libraries are distributed separately from Tna’’Srevn’Er under the
[CC BY-SA 4.0 license with the KiCad library exception](https://gitlab.com/kicad/libraries/kicad-symbols/-/blob/master/LICENSE.md).
Tna’’Srevn’Er is not affiliated with or endorsed by the KiCad project.
