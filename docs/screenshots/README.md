# Screenshots

Drop PNGs here using the filenames below and they will appear in the project
README's Screenshots section. Capture at 1440×900 or wider, in dark mode (the
app defaults to it), with `make seed && make train` already run so the charts
have data in them.

| File | Page | What it should show |
|---|---|---|
| `dashboard.png` | `/` | KPI row, per-cluster allocation bars, source health, recent anomalies |
| `vms.png` | `/inventory/vms` | The data table with a search term applied and both source badges visible |
| `vm-detail.png` | `/inventory/vms/:id` | A `growing` VM: the 180-day chart plus the forecast with its band and threshold line |
| `anomalies.png` | `/anomalies` | The timeline strip above the table, with at least one open anomaly |
| `capacity.png` | `/capacity` | Clusters with days-to-threshold, and the datastore table sorted by usage |
| `recommendations.png` | `/recommendations` | Reclaimable-resource tiles and the grouped tables |
| `assistant.png` | `/assistant` | An answered question with the "tool calls used" section expanded |
| `sources.png` | `/settings/sources` | Both sources healthy, with the Sync now control |

A good `vm-detail.png` is `DC0_H0_VM0` or `nx-vm-001`: both are assigned the
`growing` profile, so the trend and the forecast are visible rather than flat.
