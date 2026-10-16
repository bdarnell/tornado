# Releasing Tornado

Notes for maintainers. Pushing a `v*` tag runs the build workflow, which
builds the sdist and wheels and uploads them to PyPI, so everything below
happens before the tag is pushed.

## Minor releases (6.x.0)

1. Releases are made from a release branch (`branch6.x`). Pushes to it run
   the build workflow and the downstream testbed
   (`.github/workflows/downstream.yml`), which runs the test suites of the
   most popular dependents against the built package.
2. Ship at least one pre-release (`v6.x.0b1`, optionally an alpha first and
   a release candidate after) and announce it on the mailing list and in
   the GitHub release. Leave about two weeks between the first beta and the
   final release, so downstream projects and users have time to report
   problems.
3. Before each tag, run `python maint/scripts/pre_tag_checks.py <tag>` on
   the commit to be tagged. It checks the version number and release notes
   against the tag, runs the full test suite, lint, docs and coverage, and
   compares benchmarks with the previous release.

### Deprecations

A public name, argument or behavior is removed only after at least one
minor release in which it emits a `DeprecationWarning` and is documented
with a `.. deprecated::` note saying what to use instead. The API snapshot
test (`tornado/test/api_test.py`) makes any removal show up in review.

## Patch releases (6.x.y)

Patch releases have no beta, so the pre-tag checks are the main
protection against regressions. Run `pre_tag_checks.py` on the release
branch before tagging.

## Security releases

Security releases skip the public pre-release, and that is where recent
regressions have come from (6.5.3 was followed by 6.5.4, and 6.5.9 by
6.5.10). In their place:

- Run `pre_tag_checks.py` on the fix branch. This includes the property
  tests and the regression tests from earlier security fixes, so a fix
  can't silently break neighboring behavior.
- Run tornado-testbed against the fix before disclosure, so dependents
  such as Jupyter are exercised. The fix isn't in the public repository
  yet, so the testbed has to be given a build of the private branch.
- Keep each fix to the smallest behavior change that closes the hole. When
  a fix tightens behavior that applications may rely on, ship a way to opt
  out in the same release.
- Be ready to make a follow-up release within a day, and say so in the
  advisory.
