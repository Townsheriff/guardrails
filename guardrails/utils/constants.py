import re
from guardrails.classes.templating.constants_container import ConstantsContainer
from guardrails.classes.templating.namespace_template import NamespaceTemplate

# Singleton instance created on import/init
constants = ConstantsContainer()


def substitute_constants(text):
    """Substitute constants in the prompt."""
    # Substitute constants by reading the constants file.
    # Regex to extract all occurrences of ${gr.<constant_name>}
    matches = re.findall(r"\${gr\.(\w+)}", text)

    # Substitute all occurrences of ${gr.<constant_name>}
    #   with the value of the constant.
    for match in matches:
        template = NamespaceTemplate(text)
        mapping = {f"gr.{match}": constants[match]}
        text = template.safe_substitute(**mapping)

    return text
