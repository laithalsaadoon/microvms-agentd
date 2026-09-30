{#- The template towncrier.toml names for CHANGELOG.md. towncrier writes the release heading
    (`title_format`) itself, then this: a `### <Type>` list per type, in towncrier.toml's
    order. Each fragment is already the list items it adds (`all_bullets = false`), so its text
    goes in as written, and the fragments of one type arrive in issue order. Each entry names
    its issues in its own bold lead ("(#283, ARCH-1, ARCH-7, ARCH-8)."), so no issue list is
    rendered here. A type without `showcontent` (internal) is never rendered. towncrier doesn't
    read this file as a fragment, because it's the template. -#}
{% for section in sections %}
{% for category, definition in definitions.items() if category in sections[section] and definition["showcontent"] %}

### {{ definition["name"] }}

{% for text in sections[section][category] %}
{{ text }}
{% endfor %}
{% endfor %}
{% endfor %}
{#- A blank line between this release and the one below it. #}
{{ "\n" }}
