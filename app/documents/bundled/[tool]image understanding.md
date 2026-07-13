---
description: "The Image Understanding (image_understanding) built-in tool and its Tool Variables MAX_IMAGE_BYTES (max image size sent to the vision model) and MAX_IMAGE_DIMENSION (longest-side pixel cap for downscaling). How to view and change the image_understanding limits per profile."
---

# Image Understanding Tool (image_understanding)

The **Image Understanding** tool (`tool_id` `image_understanding`) sends an image
to a vision model and answers questions about it. It is on and visible by
default. Its two variables bound the image size sent to the model.

## Tool Variables

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `MAX_IMAGE_BYTES` | number | `8388608` (8 MB) | Maximum image size in bytes sent to the vision model. Larger images are downscaled when Pillow is available, otherwise rejected. |
| `MAX_IMAGE_DIMENSION` | number | `2048` | Longest-side pixel cap for downscaling oversized images (requires Pillow). |

`image_understanding` has no Tool Arguments.

## Viewing and changing these

Per-profile, three equivalent ways:

- **UI** — Settings → Tools & Skills → Image Understanding.
- **CLI** — `cremind tools set-var image_understanding MAX_IMAGE_DIMENSION=1024`;
  `cremind tools get image_understanding --json` to read current values.
- **Agent** — the assistant can run those commands via its Shell Executor.

Changes take effect on the tool's next call — no restart. See `cremind tools`
for the full configuration CLI.
