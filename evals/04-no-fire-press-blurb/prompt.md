---
description: Should-not-fire control. A plain newsletter blurb about a press release that mentions a video; the shorts-studio skill must not fire and no look-dev pipeline detail should leak into the answer.
tags: [no-fire, control]
runs: 3
max_turns: 5
timeout_seconds: 120
allowed_tools: [Read, Glob, Grep, Skill]
---

Write a two-sentence blurb for our customer newsletter about the press release below. Plain prose only, no headings or bullet points.

> **Northwind opens a Berlin office**
>
> Northwind announced today that it has opened its first European office, in Berlin, to support customers across the EU in their own time zone. The office starts with a team of twelve in support and solutions engineering and will grow to forty by the end of next year. A short launch video with the Berlin team is on the company's YouTube channel.
