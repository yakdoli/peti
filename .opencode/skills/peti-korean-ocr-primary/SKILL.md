---
name: peti-korean-ocr-primary
description: Fast and accurate Korean OCR transcription rules for Gwanbo/public-administration page images.
---

# Peti Korean OCR Primary

Use this skill for primary OCR transcription of Korean Gwanbo and public-administration PDF page images.

This mirrors the current Codex subagent transcription flow: one task is one prepared page image, already rendered at 250dpi and preprocessed for OCR. Treat each opencode invocation as a single page task, not a whole PDF.

This OCR skill is MCP-free by policy. Do not use MCP servers, shell commands, web tools, repository reads, or network retrieval. Use only the attached image and prompt context; built-in image inspection for the attached file is allowed. If image inspection uses an internal image tool, do not mention the tool in the final answer.

## Output Contract

Return exactly one JSON object and no markdown:

```json
{"text":"...","confidence":0.0,"notes":"..."}
```

The first character of the response must be `{` and the last character must be `}`.

Required fields:

- `text`: full visible page transcription, or empty string only when image inspection fails.
- `confidence`: OCR confidence from 0.0 to 1.0.
- `notes`: concise issue note, for example unreadable regions or image tool failure.

Do not include diagnostic text such as OCR engine banners, resolution estimates, markdown fences, or prose outside the JSON object.

## Task Packet Flow

- The image is attached with `--file`; inspect that image directly before answering.
- The prompt may include an `Image context` string with page number, DPI, pixel size, bbox, job name, and primary backend.
- Do not use MCP tools. Do not inspect repo files or request additional context through tools.
- Do not ask for more files. Do not attempt multi-page OCR.
- Do not write transcript files in opencode primary mode; return JSON to stdout.
- If the page is dense, continue transcribing visible text instead of summarizing.

## Core Rules

- Transcribe visible text only. Do not infer hidden, cropped, blurred, or missing text.
- Do not summarize the page and do not explain the layout outside `notes`.
- Preserve natural reading order, row grouping, labels, indentation, and line breaks when clear.
- Keep original Korean Hangul/Hanja, numbers, punctuation, date marks, list markers, quotes, brackets, and units.
- Keep official forms such as `제12609호`, `관보`, `공고제1994-1호`, `1994. 1. 5.`, `(수요일)`.
- Keep Korean address tokens exactly when visible: `서울시`, `경기도`, `제주도`, `시`, `군`, `구`, `읍`, `면`, `동`, `리`, `산`, street/lot numbers, and hyphenated parcel IDs.
- Preserve land/asset terms: `대지`, `임야`, `전`, `답`, `단독주택`, `아파트`, `근린생활시설`, `자동차`, `예금`, `채무`, `유가증권`, `회원권`, `보석`.
- Preserve measurements and money: `㎡`, `평형`, `cc`, `천원`, commas in numbers, `△` negative signs, percentages, and shares such as `1/2`.
- Preserve table headers such as `소속`, `직위`, `성명`, `본인과의 관계`, `재산의 종류`, `소재지·면적 등 권리명세`, `가액`, `비고`, `소계`, `총계`.

## Korean OCR Pitfalls

- Distinguish `공고` from `고시`; do not replace one with the other unless clearly visible.
- Distinguish `외무부`, `의무자`, `본인`, `배우자`, `장남`, `차녀`.
- Distinguish similar syllables in official text: `태/대`, `국/학`, `부/부부`, `권리/관리`, `건평/건물`.
- Do not expand acronyms or agency names from memory. If `KOTRA`, `KOICA`, or another acronym is unclear, transcribe what is visible or mark only the uncertain token.
- For names, positions, and addresses, prefer `[판독불가]` for a small uncertain token over a guessed correction.

## Speed Guidance

- Use a single image inspection pass when possible.
- Avoid chain-of-thought or prose analysis. Produce the JSON result directly.
- If a dense table is partially unreadable, transcribe all clearly visible rows and mark only unreadable tokens with `[판독불가]`.
- If the image tool fails or returns no visible text, return `{"text":"","confidence":0.0,"notes":"image tool failed or image unreadable"}`.

## Confidence Guidance

- Use `0.85` or higher only when the page is clearly readable and most table structure is preserved.
- Use `0.55` to `0.8` when most text is visible but dense tables or small characters may contain errors.
- Use below `0.5` when large regions are uncertain.
