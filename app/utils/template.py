"""
Utility functions for template rendering that can be reused across different classes.

This module provides reusable functions for building structured content with Jinja2 templates,
including list content generation, tool wrapping, and prompt cleaning.
"""

import jinja2
from typing import Dict, List


# Default template strings that can be customized
DEFAULT_LIST_CONTENT_TEMPLATE = """
{% for content in contents %}
<{{ element_name }}{{ loop.index }}>
{{ content | indent(2, True) }}
</{{ element_name }}{{ loop.index }}>
{% endfor %}
""".strip()

DEFAULT_TOOL_PROMPT_TEMPLATE = """
<{{ tool_name }}>
{{ content | indent(2, True) }}
</{{ tool_name }}>
""".strip()


def create_jinja_environment(template_dir: str, autoescape: bool = False) -> jinja2.Environment:
    """
    Create a Jinja2 environment with the specified configuration.

    :param template_dir: Directory containing template files.
    :param autoescape: Whether to enable autoescaping.
    :return: Configured Jinja2 environment.
    """
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(template_dir),
        autoescape=autoescape
    )


def build_list_content(env: jinja2.Environment,
                       element_name: str,
                       contents: List[str],
                       template_str: str = DEFAULT_LIST_CONTENT_TEMPLATE) -> str:
    """
    Builds the inner list content with numbered elements (e.g., <CONVERSATION1>indented content</CONVERSATION1>).

    :param env: Jinja2 environment to use for rendering.
    :param element_name: The inner element name (e.g., "CONVERSATION" or "TOPIC").
    :param contents: List of content strings to wrap in numbered elements.
    :param template_str: Template string to use for rendering (optional).
    :return: The rendered list content string.
    """
    template = env.from_string(template_str)
    return template.render(element_name=element_name, contents=contents)


def wrap_tool_content(env: jinja2.Environment,
                      tool_name: str,
                      content: str,
                      template_str: str = DEFAULT_TOOL_PROMPT_TEMPLATE) -> str:
    """
    Wraps content in a tool prompt section (e.g., <CONVERSATION_TOOL>...</CONVERSATION_TOOL>).

    :param env: Jinja2 environment to use for rendering.
    :param tool_name: The outer tool tag name (e.g., "CONVERSATION_TOOL" or "SENSITIVE_TOPIC_CHECK").
    :param content: Content to wrap in the tool tags.
    :param template_str: Template string to use for rendering (optional).
    :return: The rendered tool section string.
    """
    template = env.from_string(template_str)
    return template.render(tool_name=tool_name, content=content)


def build_session_list_content(env: jinja2.Environment,
                               tool_name: str,
                               element_name: str,
                               contents: List[str],
                               list_template_str: str = DEFAULT_LIST_CONTENT_TEMPLATE,
                               tool_template_str: str = DEFAULT_TOOL_PROMPT_TEMPLATE) -> str:
    """
    Builds a complete tool section by first building the list content and then wrapping it.
    Equivalent to replaceSessionListContent in the Node.js code.

    :param env: Jinja2 environment to use for rendering.
    :param tool_name: The outer tool tag name (e.g., "CONVERSATION_TOOL" or "SENSITIVE_TOPIC_CHECK").
    :param element_name: The inner element name (e.g., "CONVERSATION" or "TOPIC").
    :param contents: List of content strings to wrap in numbered elements.
    :param list_template_str: Template string for list content (optional).
    :param tool_template_str: Template string for tool wrapping (optional).
    :return: The rendered tool section string.
    """
    list_content = build_list_content(env, element_name, contents, list_template_str)
    return wrap_tool_content(env, tool_name, list_content, tool_template_str)


def combine_sections(*sections: str) -> str:
    """
    Combine multiple sections into a single string, filtering out empty ones.

    :param sections: Variable number of section strings.
    :return: Combined sections string.
    """
    return "\n".join(filter(None, sections))


def clean_prompt(prompt: str) -> str:
    """
    Cleans the prompt by removing empty lines and trimming.
    Equivalent to cleanSessionPrompt in the Node.js code.

    :param prompt: The prompt string to clean.
    :return: The cleaned prompt string.
    """
    return "\n".join([line for line in prompt.split("\n") if line.strip()]).strip()


def render_template_with_sections(template: jinja2.Template, sections: Dict[str, str]) -> str:
    """
    Renders a Jinja2 template with the provided sections.

    :param template: The Jinja2 template to render.
    :param sections: Dict with section names as keys and their content as values.
    :return: The rendered template string.
    """
    return template.render(**sections)


# Convenience functions for common tool types
def build_conversation_tool(env: jinja2.Environment, conversation_contents: List[str]) -> str:
    """
    Build a conversation tool section with conversation content.

    :param env: Jinja2 environment to use for rendering.
    :param conversation_contents: List of conversation content strings.
    :return: The rendered conversation tool section.
    """
    return build_session_list_content(env, "CONVERSATION_TOOL", "CONVERSATION", conversation_contents)


def build_data_tool(env: jinja2.Environment, data_tool_contents: List[str]) -> str:
    """
    Build a data tool section with data content.

    :param env: Jinja2 environment to use for rendering.
    :param data_tool_contents: List of data content strings.
    :return: The rendered data tool section.
    """
    return build_session_list_content(env, "DATA_TOOL", "TOOL", data_tool_contents)


def build_sensitive_topic_check(env: jinja2.Environment, policy_check_contents: List[str]) -> str:
    """
    Build a sensitive topic check section with policy content.

    :param env: Jinja2 environment to use for rendering.
    :param policy_check_contents: List of policy check content strings.
    :return: The rendered sensitive topic check section.
    """
    return build_session_list_content(env, "SENSITIVE_TOPIC_CHECK", "TOPIC", policy_check_contents)


# Template factory for creating custom tool builders
def create_tool_builder(tool_name: str, element_name: str):
    """
    Creates a tool builder function for a specific tool type.

    :param tool_name: The outer tool tag name.
    :param element_name: The inner element name.
    :return: A function that builds the tool section.
    """
    def build_tool(env: jinja2.Environment, contents: List[str]) -> str:
        return build_session_list_content(env, tool_name, element_name, contents)

    build_tool.__name__ = f"build_{tool_name.lower()}_tool"
    build_tool.__doc__ = f"""
    Build a {tool_name.lower()} tool section with {element_name.lower()} content.

    :param env: Jinja2 environment to use for rendering.
    :param contents: List of content strings.
    :return: The rendered {tool_name.lower()} tool section.
    """

    return build_tool
