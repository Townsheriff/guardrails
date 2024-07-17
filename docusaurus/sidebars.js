/**
 * Creating a sidebar enables you to:
 - create an ordered group of docs
 - render a sidebar for each doc of that group
 - provide next/previous navigation

 The sidebars can be generated from the filesystem, or explicitly defined here.

 Create as many sidebars as you want.
 */

// populate examples from examples folder. Only include .mdx and .md files
const { triggerAsyncId } = require("async_hooks");
const fs = require("fs");

// @ts-check
/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */

// get examples from the file called examples-toc.json

const examples = JSON.parse(
  fs.readFileSync("./docusaurus/examples-toc.json", "utf8")
).find((x) => x.label === "Examples");

const sidebars = {
  // By default, Docusaurus generates a sidebar from the docs folder structure
  // tutorialSidebar: [{ type: "autogenerated", dirName: "." }],
  docsSidebar: [
    {
      type: "doc",
      id: "index",
      label: "Introduction",
    },
    {
      type: "category",
      label: "Getting started",
      collapsed: false,
      items: [
        "getting_started/quickstart",
        "getting_started/why_use",
        "getting_started/ai_validation",
        "getting_started/ml_based",
        "getting_started/structured_data",
        "getting_started/deploying",
        "getting_started/contributing",
        "getting_started/help",
        "faq"
      ],
    },
    {
      type: "category",
      label: "Concepts",
      collapsed: false,
      items: [
        "concepts/guard",
        "concepts/guardrails",
        "concepts/hub",
        "concepts/remote_validation_inference",
        {
          type: "category",
          label: "Streaming",
          collapsed: true,
          items: [
            "concepts/streaming",
            "concepts/async_streaming",
            "concepts/streaming_structured_data",
          ],
        },
        "concepts/parallelization",
        "concepts/logs",
        "concepts/telemetry",
        "concepts/error_remediation",
      ],
    },
    {
      type: "category",
      label: "Examples",
      collapsed: false,
      items: ["examples/chatbot", "examples/summarizer", {
        type: "link",
        label: "More Examples",
        href: "https://github.com/guardrails-ai/guardrails/tree/main/docs/examples"
      }],
    },
    {
      type: "category",
      label: "Cookbooks",
      collapsed: true,
      items: [
        "cookbooks/using_llms",
      ],
    },
    {
      type: "category",
      label: "Integrations",
      collapsed: true,
      items: [
        // "integrations/azure_openai",
        "integrations/langchain",
        {
          type: "category",
          label: "Telemetry",
          collapsed: true,
          items: [
            {
              type: "link",
              label: "Arize AI",
              href: "https://docs.arize.com/arize/large-language-models/guardrails",
            },
            "integrations/telemetry/grafana",
          ],
        },

        // "integrations/openai_functions",
      ],
    },
    {
      type: "category",
      label: "Server",
      collapsed: true,
      items: [
        "server/rest_api",
      ],
    },
    {
      type: "category",
      label: "Migration Guides",
      collapsed: true,
      items: [
        "migration_guides/0-5-migration",
        "migration_guides/0-4-migration",
        "migration_guides/0-3-migration",
        "migration_guides/0-2-migration",
      ],
    },
  ],
};

module.exports = sidebars;
