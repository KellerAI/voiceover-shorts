---
description: Style selection from the registry for a UI article that will be published. The only `ui` style (C) uses uncleared fonts, so the correct outcome is the D fallback.
tags: [core-path, style-select, knowledge]
runs: 3
max_turns: 12
timeout_seconds: 300
allowed_tools: [Read, Glob, Grep, Skill]
---

I'm making a YouTube Short from the article below, and the result **will be published** on our public channel. The narration WAV and its words.json are already recorded. Content-wise this is purely a product-UI walkthrough: no data, no metrics, no process steps. For now I only need the look-dev style decision. Do not cut audio, run a workflow, or render anything. Tell me which style to run and why, and which styles you ruled out and why.

Article:

> **Northwind ships a redesigned dashboard**
>
> Northwind today released a redesigned product dashboard. The new layout moves the navigation sidebar into a collapsible rail, adds a command palette on Cmd-K, and replaces the settings modal with an inline settings pane. Buttons, form fields and tables now share one component library, and every screen supports dark mode.
