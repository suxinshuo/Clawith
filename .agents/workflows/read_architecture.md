---
description: Read the Clawith architecture and feature specification at the start of every session
---

# Read Feature Spec

At the start of every conversation about Clawith, always read the full architecture specification document first before doing any work. This ensures you are aware of all existing functionality and won't break or duplicate it when adding new features. It also keeps you aligned with the core design philosophies and terminology.

// turbo
1. Read the feature specification:
```
cat ARCHITECTURE_SPEC.md
```

2. Confirm you have read and understood the full architecture spec, then ask the user what they would like to work on.

## After Implementing New Features

After completing any feature addition or modification, **always update ARCHITECTURE_SPEC.md**:
- Add/modify the relevant section describing the new feature or architectural change
- Add a row to the "Changelog" table at the bottom with today's date and a brief summary
- Keep descriptions concise but complete enough to understand what was built and why. Ensure you maintain the pure English format.
