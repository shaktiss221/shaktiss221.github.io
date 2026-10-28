---
week: 10
phase: Agents and MCP
title: MCP and multi-agent systems
goal: Expose your tools through the Model Context Protocol, use them from AI apps, and learn how multi-agent systems are built.
---

## What MCP is
learn: 20 | Model Context Protocol: introduction and architecture | https://modelcontextprotocol.io
do: 7 | Install one reference server (filesystem or git) in your AI app and use it once.
note: 3 | Write how MCP relates to the tool calling you built in week 4.
also: Reference MCP servers | https://github.com/modelcontextprotocol/servers

## Your first MCP server
learn: 5 | MCP Python SDK: README | https://github.com/modelcontextprotocol/python-sdk
do: 22 | Wrap your read-only ledger tools as an MCP server.
note: 3 | Note anything in the SDK that surprised you.
also: Java option: MCP Java SDK | https://github.com/modelcontextprotocol/java-sdk

## Use it from an AI app
do: 25 | Connect your MCP server to Claude Desktop, Claude Code or another MCP client and ask questions about the ledger in plain English.
note: 5 | What confused the model? Fix the tool descriptions.

## Code execution with MCP
learn: 20 | Anthropic, Code execution with MCP | https://www.anthropic.com/research/code-execution-with-mcp
do: 7 | Find one workflow of yours where letting the agent write code would use fewer tokens than many separate tool calls.
note: 3 | What risks does code execution add?

## Multi-agent systems
learn: 25 | Anthropic, How we built our multi-agent research system | https://www.anthropic.com/research/multi-agent-research-system
note: 5 | When is one agent with good tools better than several agents?

## Skills: reusable know-how for agents
learn: 15 | Anthropic, Equipping agents for the real world with Agent Skills | https://www.anthropic.com/research/equipping-agents-for-the-real-world-with-agent-skills
do: 12 | Write a skill (a folder with instructions and scripts) for a task you repeat at work, such as a review checklist for payment code.
note: 3 | Use it once and note what was missing.

## Week 10 review: ship the MCP server
do: 20 | Write a README for your MCP ledger server with setup steps, a demo transcript and its security rules (read-only, no personal data in responses).
note: 10 | Publish it on GitHub. It is your first portfolio piece.
