# Project Requirements

## Windows Share Output

- Completed KTV packages must be written to the Windows share under `/Volumes/Karaoke Shared/imported/`.
- The Mac's local cache is a working cache, not a replacement for the required shared output.
- Successful exports require complete artifacts and checksum manifests under `manifests/`.
- Publication errors must be recorded under `failed/` and remain retryable.
- Do not describe this feature as complete until real generated files are present on the share and their checksums have been verified.

## Following User Requirements

- Do not replace an explicit user requirement with a preferred architecture or an assumed best practice.
- If a technical recommendation appears to conflict with a requirement, preserve the requirement and ask before changing its meaning.
- Verify observable acceptance criteria, especially requested filesystem destinations, before reporting completion.
- Do not create placeholder directories or partial infrastructure and imply that the requested end-to-end behavior is implemented.

## Recorded Failure

The shared folders were initially created without wiring completed preparation output into them. The local cache was then incorrectly described as the intended final storage despite the explicit requirement to save generated KTV files to the Windows share. There was no conflicting instruction and no ambiguity that required this reinterpretation. This was an implementation and verification failure and must not be repeated.
