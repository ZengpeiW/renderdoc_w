#pragma once

#include <QString>

// Minimal Markdown to HTML conversion for the AI panel (Qt 5.9 has no QTextDocument::setMarkdown).
// Covers headings, paragraphs, lists, fenced code, inline code, bold, links and simple tables.
namespace AgentMarkdown
{
QString ToHtml(const QString &markdown, const QString &referenceToken = QString());
QString InlineToHtml(const QString &text);
QString Escape(const QString &text);
}
