# Missing HTTP Redirect Functionality in Buildbot WWW Resource Module

## Current Issue

The Buildbot web interface is missing critical HTTP redirect functionality that prevents proper authentication flows and resource redirection. Several components in the authentication system (`auth.py`, `avatar.py`, `oauth2.py`) are attempting to use `resource.Redirect` exceptions and `resource.RedirectResource` classes that are not currently implemented in the `buildbot.www.resource` module.

This causes authentication workflows to fail, avatar requests to malfunction, and OAuth2 login processes to break, as these components cannot properly redirect users to appropriate URLs during the authentication process.

## Expected Behavior

The `buildbot.www.resource` module should provide a complete HTTP redirect mechanism consisting of:

1. **Redirect Exception Class**: A custom exception class that extends Twisted's `Error` class to represent HTTP 302 redirects, carrying the target URL information.

2. **Redirect Error Handling**: The `Resource.asyncRenderHelper` method should include proper error handling to catch redirect exceptions and execute the actual HTTP redirect response.

3. **RedirectResource Class**: A simple resource class that immediately redirects requests to a configured base path, useful for creating redirect endpoints.

The redirect functionality must integrate seamlessly with Twisted's web framework and support the existing authentication and avatar systems that depend on raising redirect exceptions to trigger HTTP redirects.