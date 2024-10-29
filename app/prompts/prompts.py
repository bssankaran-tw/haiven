# © 2024 Thoughtworks, Inc. | Licensed under the Apache License, Version 2.0  | See LICENSE.md file for permissions.
import os
import sys
from typing import List

import frontmatter
from langchain.prompts import PromptTemplate
from knowledge.markdown import KnowledgeBaseMarkdown


class PromptList:
    def __init__(
        self,
        interaction_type,
        knowledge_base: KnowledgeBaseMarkdown,
        variables=[],
        root_dir="teams",
    ):
        data_sources = {
            "diagrams": {
                "dir": root_dir + "/prompts/diagrams",
                "title": "Diagrams",
            },
            "brainstorming": {
                "dir": root_dir + "/prompts/brainstorming",
                "title": "Brainstorming",
            },
            "chat": {
                "dir": root_dir + "/prompts/chat",
                "title": "Chat",
            },
            "guided": {"dir": root_dir + "/", "title": "Guided"},
        }

        self.interaction_pattern_name = data_sources[interaction_type]["title"]

        base_path = getattr(sys, "_MEIPASS", os.path.abspath("."))
        directory = os.path.join(base_path, data_sources[interaction_type]["dir"])
        # directory = data_sources[interaction_type]["dir"]
        prompt_files = sorted(
            [f for f in os.listdir(directory) if f.endswith(".md") and f != "README.md"]
        )
        self.prompts = [
            frontmatter.load(os.path.join(directory, filename))
            for filename in prompt_files
        ]

        self.knowledge_base = knowledge_base
        self.extra_variables = variables

        for prompt in self.prompts:
            if "title" not in prompt.metadata:
                prompt.metadata["title"] = "Unnamed use case"
            if "system" not in prompt.metadata:
                prompt.metadata["system"] = "You are a useful assistant"
            if "categories" not in prompt.metadata:
                prompt.metadata["categories"] = []

    def get_title_id_tuples(self):
        tuples = [
            (
                prompt.metadata.get("title", "Unnamed use case"),
                prompt.metadata.get("identifier"),
            )
            for prompt in self.prompts
        ]

        sorted_tuples = sorted(tuples, key=lambda x: x[0])

        return sorted_tuples

    def get(self, identifier):
        for prompt in self.prompts:
            if prompt.metadata.get("identifier") == identifier:
                return prompt
        return None

    def create_template(
        self, active_knowledge_context: str, identifier: str
    ) -> PromptTemplate:
        prompt_data = self.get(identifier)
        if not prompt_data:
            raise ValueError(f"Prompt {identifier} not found")

        prompt_text = prompt_data.content
        variables = (
            ["user_input"]
            + self.knowledge_base.get_context_keys(active_knowledge_context)
            + self.extra_variables
        )
        return PromptTemplate(input_variables=variables, template=prompt_text)

    def create_and_render_template(
        self, active_knowledge_context, identifier, variables, warnings=None
    ):
        if active_knowledge_context:
            knowledge_and_input = {
                **self.knowledge_base.get_knowledge_content_dict(
                    active_knowledge_context
                ),
                **variables,
            }
        else:
            knowledge_and_input = {**variables}

        template = self.create_template(active_knowledge_context, identifier)
        template.get_input_schema()
        template.dict()

        # make sure all input variables are present for template rendering (as it will otherwise fail)
        for key in template.input_variables:
            if key not in knowledge_and_input:
                knowledge_and_input[str(key)] = (
                    "None provided, please try to help without this information."  # placeholder for the prompt
                )
                if key != "user_input":
                    message = f"No context selected, no '{key}' added to the prompt."  # message shown to the user
                    if warnings is not None:
                        warnings.append(message)

        rendered = template.format(**knowledge_and_input)
        return rendered, template

    def filter(self, filter_categories: List[str]):
        if filter_categories is not None:
            self.prompts = list(
                filter(
                    lambda prompt: (
                        not prompt.metadata["categories"]
                        or any(
                            category in prompt.metadata["categories"]
                            for category in filter_categories
                        )
                    ),
                    self.prompts,
                )
            )

    def render_prompt(
        self,
        active_knowledge_context: str,
        prompt_choice: str,
        user_input: str,
        additional_vars: dict = {},
        warnings=None,
    ) -> str:
        if prompt_choice is not None:
            vars = additional_vars
            vars["user_input"] = user_input
            rendered, template = self.create_and_render_template(
                active_knowledge_context, prompt_choice, vars, warnings=warnings
            )
            return rendered, template
        return "", None

    def get_knowledge_used_keys(self, active_knowledge_context: str, identifier: str):
        if identifier is not None:
            template = self.create_template(active_knowledge_context, identifier).dict()
            return template["input_variables"]

    def get_default_context(self, prompt_choice: str):
        return self.get(prompt_choice).metadata.get("context", "None")

    def get_knowledge_used(self, prompt_choice: str, active_knowledge_context: str):
        prompt = self.get(prompt_choice)
        if prompt is not None:
            knowledge_keys = self.get_knowledge_used_keys(
                active_knowledge_context, prompt_choice
            )
            knowledge = []
            for key in knowledge_keys:
                knowledge_entry = self.knowledge_base.get_knowledge_document(
                    active_knowledge_context, key
                )
                if knowledge_entry:
                    knowledge.append(knowledge_entry.metadata)

            return knowledge

    def render_help_markdown(self, prompt_choice: str, context_selected: str):
        prompt = self.get(prompt_choice)
        if prompt is not None:
            title = f"## {prompt.metadata.get('title')}"

            prompt_description = prompt.metadata.get("help_prompt_description", "")
            prompt_markdown = (
                f"**Description:** {prompt_description}" if prompt_description else ""
            )
            user_input_description = prompt.metadata.get("help_user_input", "")
            user_input_markdown = (
                f"**User input:** {user_input_description}"
                if user_input_description
                else ""
            )
            knowledge_used = self.get_knowledge_used(prompt_choice, context_selected)

            knowledge_used_markdown = (
                "**Knowledge used:** "
                + ", ".join(
                    f"_{knowledge['title']}_ from _{context_selected}_"
                    for knowledge in knowledge_used
                )
                if knowledge_used
                else None
            )

            sample_input = prompt.metadata.get("help_sample_input", "")
            sample_input_markdown = (
                f"**Sample input:** {sample_input}" if sample_input else ""
            )

            return (
                f"{title}\n{prompt_markdown}\n\n{user_input_markdown}\n\n{sample_input_markdown}",
                knowledge_used_markdown,
            )
        return None

    def render_prompts_summary_markdown(self):
        prompts_summary = ""
        for prompt in self.prompts:
            title = prompt.metadata.get("title")
            description = prompt.metadata.get("help_prompt_description")
            if title and description:
                prompt_summary = f"- **{title}**: {description}\n"
                prompts_summary += prompt_summary
        return prompts_summary
