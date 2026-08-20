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

- Import and align the top and bottom board images.
- Place components, footprints, pads, and pins, including through-hole devices.
- Recreate point-to-point connections and organize them into nets.
- Inspect the board through the schematic, connections, net, and BOM views.
- Automatically generate the electrical schematic from the defined links.

## Screenshots

<p align="center">
  <img src="ressources/Screenshot%202026-08-14%20at%2008.26.01.png" alt="Top board view" width="48%">
  <img src="ressources/Screenshot%202026-08-20%20at%2018.06.49.png" alt="Both board faces" width="48%">
</p>
<p align="center">
  <img src="ressources/Screenshot%202026-08-20%20at%2018.06.41.png" alt="Automatically generated schematic" width="48%">
  <img src="ressources/Screenshot%202026-08-14%20at%2008.26.45.png" alt="Connections table" width="48%">
</p>
