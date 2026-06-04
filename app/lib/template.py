from typing import Dict, List, Optional
import os

from app.utils.template import build_conversation_tool, build_list_content, build_sensitive_topic_check, build_data_tool, build_session_list_content, combine_sections, create_jinja_environment, render_template_with_sections, clean_prompt


class AssistantTemplateRenderer:
    """
    A class that encapsulates template rendering functionality for building NLG prompts.

    This class provides methods to build structured prompts with various sections like
    conversations, service data, device information, and policy checks.
    """

    def __init__(self, template_dir: Optional[str] = None, template_name: str = 'assistant.jinja'):
        """
        Initialize the AssistantTemplateRenderer with Jinja2 environment and load the main template.

        :param template_dir: Directory containing template files. If None, uses the templates directory.
        :param template_name: Name of the main template file.
        """
        if template_dir is None:
            # Get the parent directory of this file and join with 'templates'
            current_dir = os.path.dirname(__file__)
            template_dir = os.path.join(os.path.dirname(current_dir), 'templates')

        # Create Jinja2 environment using utility function
        self.env = create_jinja_environment(template_dir)

        # Load the main NLG template from external file
        self.nlg_template = self.env.get_template(template_name)

    def build_list_content(self, element_name: str, contents: List[str]) -> str:
        """
        Builds the inner list content with numbered elements (e.g., <CONVERSATION1>indented content</CONVERSATION1>).
        Equivalent to the inner part of replaceSessionListContent in the Node.js code.

        :param element_name: The inner element name (e.g., "CONVERSATION" or "TOPIC").
        :param contents: List of content strings to wrap in numbered elements.
        :return: The rendered list content string.
        """
        return build_list_content(self.env, element_name, contents)

    def replace_session_list_content(self, tool_name: str, element_name: str, contents: List[str]) -> str:
        """
        Replaces the content in a tool prompt section by first building the list content and then wrapping it.
        Equivalent to replaceSessionListContent in the Node.js code, but using Jinja for rendering.

        :param tool_name: The outer tool tag name (e.g., "CONVERSATION_TOOL" or "SENSITIVE_TOPIC_CHECK").
        :param element_name: The inner element name (e.g., "CONVERSATION" or "TOPIC").
        :param contents: List of content strings to wrap in numbered elements.
        :return: The rendered tool section string.
        """
        return build_session_list_content(self.env, tool_name, element_name, contents)

    def build_conversation_tool(self, conversation_contents: List[str]) -> str:
        """
        Build a conversation tool section with conversation content.

        :param conversation_contents: List of conversation content strings.
        :return: The rendered conversation tool section.
        """
        return build_conversation_tool(self.env, conversation_contents)

    def build_data_tool(self, data_tool_contents: List[str]) -> str:
        """
        Build a service data tool section with service data content.

        :param data_tool_contents: List of service data content strings.
        :return: The rendered service data tool section.
        """
        return build_data_tool(self.env, data_tool_contents)

    def build_sensitive_topic_check(self, policy_check_contents: List[str]) -> str:
        """
        Build a sensitive topic check section with policy content.

        :param policy_check_contents: List of policy check content strings.
        :return: The rendered sensitive topic check section.
        """
        return build_sensitive_topic_check(self.env, policy_check_contents)

    def combine_tools(self, *tools: str) -> str:
        """
        Combine multiple tool sections into a single tools string.

        :param tools: Variable number of tool section strings.
        :return: Combined tools string.
        """
        return combine_sections(*tools)

    def build_nlg_prompt(self, sections: Dict[str, str]) -> str:
        """
        Builds the final NLG prompt by rendering the main Jinja template with the provided sections.
        Sections not provided are automatically omitted.
        Equivalent to sequential replaceSession calls in the Node.js code, but using Jinja for conditional rendering.

        :param sections: Dict with keys like "introduce_assistant", "devices_info", etc., and their content as values.
        :return: The rendered prompt string.
        """
        return render_template_with_sections(self.nlg_template, sections)

    def clean_session_prompt(self, prompt: str) -> str:
        """
        Cleans the prompt by removing empty lines and trimming.
        Equivalent to cleanSessionPrompt in the Node.js code.

        :param prompt: The prompt string to clean.
        :return: The cleaned prompt string.
        """
        return clean_prompt(prompt)

    def render_complete_prompt(self,
                               introduce_assistant: Optional[str] = None,
                               main_task: Optional[str] = None,
                               requirements: Optional[str] = None,
                               assistant_knowledge: Optional[str] = None,
                               internet_search_results: Optional[str] = None,
                               devices_info: Optional[str] = None,
                               conversation_contents: Optional[List[str]] = None,
                               data_tool_contents: Optional[List[str]] = None,
                               policy_check_contents: Optional[List[str]] = None,
                               response_format: Optional[str] = None,
                               language: Optional[str] = None,
                               reasoning_answer: Optional[str] = None,
                               custom_tools: Optional[str] = None) -> str:
        """
        Convenience method to render a complete prompt with all sections.

        :param introduce_assistant: Assistant introduction text.
        :param main_task: Main task description.
        :param requirements: Requirements text.
        :param assistant_knowledge: Assistant knowledge text.
        :param internet_search_results: Internet search results text.
        :param devices_info: Device information text.
        :param conversation_contents: List of conversation contents for conversation tool.
        :param data_tool_contents: List of service data contents for service data tool.
        :param policy_check_contents: List of policy contents for sensitive topic check.
        :param reasoning_answer: Reasoning answer text.
        :param response_format: Response format instructions.
        :param language: Language specification.
        :param custom_tools: Custom tools string (overrides auto-generated tools).
        :return: The complete rendered and cleaned prompt.
        """
        sections = {}

        # Add simple sections
        if introduce_assistant:
            sections["introduce_assistant"] = introduce_assistant
        if main_task:
            sections["main_task"] = main_task
        if requirements:
            sections["requirements"] = requirements
        if assistant_knowledge:
            sections["assistant_knowledge"] = assistant_knowledge
        if internet_search_results:
            sections["internet_search_results"] = internet_search_results
        if devices_info:
            sections["devices_info"] = devices_info
        if response_format:
            sections["response_format"] = response_format
        if language:
            sections["language"] = language
        if reasoning_answer:
            sections["reasoning_answer"] = reasoning_answer

        # Build tools section
        tools = []
        if conversation_contents:
            tools.append(self.build_conversation_tool(conversation_contents))
        if data_tool_contents:
            tools.append(self.build_data_tool(data_tool_contents))

        if custom_tools:
            sections["tools"] = custom_tools
        elif tools:
            sections["tools"] = self.combine_tools(*tools)

        # Build policy section
        if policy_check_contents:
            sections["policy"] = self.build_sensitive_topic_check(policy_check_contents)

        # Render and clean the prompt
        prompt = self.build_nlg_prompt(sections)
        return self.clean_session_prompt(prompt)
