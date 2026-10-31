# The Find-a-Line logo

An A-frame in perspective: the line rises from an anchor on the ground in front of the far leg, hangs under
the frame, passes behind the near leg and writes "line" in one stroke; the dot of the "i" and the end of the
"e" are anchors. "Find" is Barlow Condensed Bold (SIL Open Font License 1.1) converted to outlines, so the
files need no font.

| File | Use |
| --- | --- |
| `find-a-line-logo.svg` | The logo; light or dark colours by `prefers-color-scheme` (browsers, favicons) |
| `find-a-line-logo-light.svg`, `-dark.svg` | The logo for a light or a dark background (editors that ignore the media query) |
| `find-a-line-mark.svg` | The mark alone (small sizes); light or dark by `prefers-color-scheme` |
| `find-a-line-mark-light.svg`, `-dark.svg` | The mark for a light or a dark background |

Colours are the app's tokens (`viewer/src/ui/tokens.css`):

| | Light | Dark |
| --- | --- | --- |
| Ink (`--ink`) | `#17201c` | `#e5ebe7` |
| Line (`--clear`) | `#e10fd6` | `#ff4cf4` |

The app uses the same drawings coloured by its tokens: `viewer/src/ui/logo/` (`logo.svg`, `mark.svg`,
`favicon.svg` = `find-a-line-mark.svg`). A change of the logo changes both folders.
