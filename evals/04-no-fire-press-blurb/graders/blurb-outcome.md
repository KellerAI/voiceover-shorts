---
type: llm
focus: last_message
weight: 2
---
The user asked for a two-sentence newsletter blurb about this press release, quoted verbatim:

> **Northwind opens a Berlin office**
>
> Northwind announced today that it has opened its first European office, in Berlin, to support customers across the EU in their own time zone. The office starts with a team of twelve in support and solutions engineering and will grow to forty by the end of next year. A short launch video with the Berlin team is on the company's YouTube channel.

Pass only if every claim below holds in the final answer:

1. The blurb is two sentences of plain prose, with no headings or bullet points. A short lead-in line such as "Here's a blurb:" outside the blurb is acceptable.
2. It says Northwind opened an office in Berlin.
3. It states no fact that contradicts the press release quoted above (for example, a different city, team size or growth target). Restating a fact from the quote, such as EU customers getting support in their own time zone or the launch video on YouTube, is not a contradiction.
4. The response contains no video-production or pipeline content: it does not offer to make, cut, style or render a video, and it does not mention video styles, narration timings, word timings, beats, render plans or tools. Mentioning facts from the press release itself, such as the company's launch video, is allowed.

Fail if it names a city other than Berlin, a starting team other than twelve, a target other than forty by the end of next year, or offers to make, cut, style or render a video.
