# Application configuration

Tnasrevner reads optional `config.yaml` from its launch directory. Configuration
is application-level and is never stored in a `.revp` project archive.

Colors are configured under `colors`. Values may use any Qt-compatible color
string, such as `#3b82f6` or `red`:

```yaml
colors:
  connected_pad: "#3b82f6"
  unconnected_pad: "#ff0000"
  new_connected_pad: "#00ff00"
  connected_pad_1: "#3b82f6"
  unconnected_pad_1: "#ffff00"
  connection_preview: "#66c2ff"
  connection_line: "#ffffff"
  selected_terminal: "#bbf7d0"
  schematic_net: "#e4b363"
  selected_schematic_net: "#66c2ff"
```

Open the configuration tab from **Application → Configuration…** or the
gear button in the Tools palette. The page exposes these supported keys with
color pickers. Changes apply immediately; use **Save configuration** to persist
them. Missing files and invalid entries use built-in defaults and are reported
through application logging.
