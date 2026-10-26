# Releases

The CI workflow validates pull requests and pushes to main. Published stable
GitHub releases also publish container images to ghcr.io/lovellyn/runlet.

## Publish a version

Keep pyproject.toml and CHANGELOG.md consistent. Commit
the release changes, tag that commit v0.3.1, and publish a GitHub release for
that tag. Stable tags must be vMAJOR.MINOR.PATCH and match the package version.

The workflow first checks that the stable tag matches pyproject.toml, then runs
tests and the amd64 container smoke test. After those pass, it builds and
pushes linux/amd64 and linux/arm64 images, with SBOM/provenance attestations.
The ARM64 variant is built during publishing; the included runtime smoke test
currently exercises amd64.

For version 0.3.1, the generated tags are:

- 0.3.1
- 0.3
- latest

Prereleases and manual CI runs do not publish images. Publish stable releases in
version order so latest and minor tags move forward.

Verify an anonymous pull of the published image before deployment. The quickstart
and Hugging Face wrapper use latest, so no version-update commit is needed.
Rebuild deployments with a fresh base-image pull to adopt the new release.

## Checks

~~~bash
python scripts/check_release.py v0.3.1
docker build --tag runlet:test .
python scripts/smoke_container.py --image runlet:test
~~~
