# Missing Link Entity Converter Function

## Current Issue

The Wagtail rich text editor's contentstate converter is missing a critical `link_entity` function that handles the conversion of link entities from Draft.js contentstate format to HTML database format. This missing functionality breaks the rich text editor's ability to properly save and render both internal page links and external links.

Currently, when the rich text editor attempts to convert contentstate data containing links back to HTML for database storage, the system fails with an import error because the `link_entity` function cannot be found. This prevents users from creating or editing content that contains any type of links in the rich text editor.

## Expected Behavior

The system should provide a `link_entity` function in the `wagtail.admin.rich_text.converters.contentstate` module that:

1. **Handles internal page links**: Converts contentstate link entities with an `id` property to HTML anchor elements with `linktype="page"` and `id` attributes
2. **Handles external links**: Converts contentstate link entities with a `url` property to HTML anchor elements with `href` attributes  
3. **Preserves link content**: Maintains the link text/children in the converted HTML output
4. **Integrates with the converter system**: Functions as an entity decorator in the contentstate-to-HTML conversion pipeline

The function should accept a `props` parameter containing the entity data and children, and return a properly formatted DOM element that can be serialized to HTML for database storage.