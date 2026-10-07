---
name: Recipe
about: A prompt, check or host profile that worked for you and could ship with gravedecay
labels: recipe
---

**Kind**

- [ ] Prompt (`prompts/*.txt`)
- [ ] Check detection (a test entry point the runner should find at the base commit)
- [ ] Host profile (`profiles/<host>.sh`)

**What it does**

One paragraph. For a prompt, the kind of repository it is for and what the morning card should show. For check detection, the file and command. For a host profile, the hardware quirk and why the mitigation is needed.

**The recipe**

```
# paste the prompt, the detection rule, or the profile_apply() body
```

**Evidence**

The verdict line from `grave agents jobs` or the card, the `grave doctor` line that verifies it, or the command that proves the quirk is gone.
