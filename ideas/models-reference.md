# Model candidates for the Flash GPU layer (stash for later)

Our Flash LLM (`flash_llm.py`) is model-agnostic — set `DND_LLM_MODEL` to any HF model. Right
now it's `Qwen/Qwen2.5-1.5B-Instruct` (generic, reliable). These D&D-tuned models could be
drop-in upgrades:

| Model | Use | Notes |
|---|---|---|
| **chendren/dnd-unified-1.5b** | text — world-gen + NPC dialogue | **Best candidate.** D&D-tuned, 1.5B = same size as our Qwen → drop-in via DND_LLM_MODEL. Could serve BOTH world-gen AND ask_npc from one endpoint, D&D-specialized. Verify it does instruct/chat + JSON. |
| **Neshi245/DnDmodel** | text — D&D content | Evaluate vs dnd-unified; check size/format. |
| **0xJustin/Dungeons-and-Diffusion** | IMAGE — character/scene/map art | The image model for the (deprioritized) art beat. Stable-Diffusion-based, D&D-tuned. For when we wire GPU art via Flash image gen. |

## How they slot in (no rework needed)
- **Swap the world-gen / NPC model:** set `DND_LLM_MODEL=chendren/dnd-unified-1.5b` on the
  Flash endpoint env. Same `flash_llm.py` infra; one endpoint can serve world-gen + NPC dialogue.
- **NPC conversation is ALREADY built + stubbed** (`inference.py` ask_npc, persona + memory).
  To go live: point it at the same Flash LLM endpoint (FLASH_NPC=1 + endpoint id). A D&D-tuned
  model would make NPC voices noticeably better than generic Qwen.
- **Image art:** when we do the art beat, mint a second Flash endpoint with Dungeons-and-Diffusion
  (image gen → ASCII for terminal / inline image for GUI).

## Plan note
Verify the GENERIC model works for structured world-gen FIRST (reliability baseline), THEN try
swapping to a D&D-tuned model for quality. Don't chase model quality before the pipeline is proven.
NPC conversation test = trivial once the LLM endpoint is verified (same infra, different prompt/skill).
