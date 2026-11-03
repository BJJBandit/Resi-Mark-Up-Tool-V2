# Test fixtures

Past jobs used as regression tests: the architectural PDF is the input, the Arete
structural set is the answer key. `manifest.yaml` (committed) describes each job and the
values the engines are expected to reproduce.

The PDFs are large (up to 21 MB each) and are **git-ignored**: Git LFS uploads are
blocked from the cloud sandbox this tool is developed in. Keep them in a synced folder
(OneDrive / SharePoint `Resi Mark-Up Tool/fixtures`) and copy them in. Folder layout:

```
fixtures/<job_id>/architectural.pdf
fixtures/<job_id>/structural_arete.pdf
```

Run the regression suite with `pytest tests/ -m fixtures` once the files are present;
tests skip cleanly when a fixture is missing.
