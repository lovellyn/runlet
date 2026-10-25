# Releases

The included workflow validates pull requests and pushes to main. It publishes
containers only for a published, non-prerelease GitHub release.

## First repository setup

1. Upload the source to [lovellyn/runlet](https://github.com/lovellyn/runlet),
   using main as the default branch.
2. Enable GitHub Actions and permit its GITHUB_TOKEN to write packages.
   The publish job requests packages: write; no registry password is needed.
3. Enable private vulnerability reporting in the repository's Security settings.
4. Review LICENSE and the 0.3.0 release notes before publishing.
5. Let the CI workflow complete, including its Docker smoke test.

For lovellyn/runlet, the image is ghcr.io/lovellyn/runlet. The workflow derives
this name from GITHUB_REPOSITORY, so forks use their own owner/repository.

## Publish a version

Keep pyproject.toml, CHANGELOG.md, and deployment examples consistent. Commit
the release changes, tag that commit v0.3.0, and publish a GitHub release for
that tag. Stable tags must be vMAJOR.MINOR.PATCH and match the package version.

The workflow reruns tests and the amd64 container smoke test, then builds and
pushes linux/amd64 and linux/arm64 images, with SBOM/provenance attestations.
The ARM64 variant is built during publishing; the included runtime smoke test
currently exercises amd64.

For version 0.3.0, the generated tags are:

- 0.3.0
- 0.3
- latest

Prereleases and manual CI runs do not publish images. Publish stable releases in
version order so latest and minor tags move forward. Prefer immutable image
digests for deployments requiring a fixed artifact.

After the first push, make the package public in its GitHub package settings.
A public repository does not by itself guarantee a public GHCR package. Test an
anonymous pull before using the image in a public deployment template.

Update the Hugging Face template's image version/digest when selecting a new
release. There is no automatic deployment to hosting services.

## Checks

~~~bash
python scripts/check_release.py v0.3.0
docker build --tag runlet:test .
python scripts/smoke_container.py --image runlet:test
~~~
