#include "AgentMarkdown.h"
#include <QRegularExpression>
#include <QStringList>

namespace
{
const char *CodeStyle = "font-family: Consolas, 'Courier New', monospace;";

QString BoldAndLinks(const QString &escaped)
{
  static const QRegularExpression bold(lit("\\*\\*(.+?)\\*\\*"));
  static const QRegularExpression link(lit("\\[([^\\]]+)\\]\\(([^)\\s]+)\\)"));
  QString text = escaped;
  text.replace(bold, lit("<b>\\1</b>"));
  text.replace(link, lit("<a href=\"\\2\">\\1</a>"));
  return text;
}

bool IsTableRow(const QString &line)
{
  QString trimmed = line.trimmed();
  return trimmed.startsWith(QLatin1Char('|')) && trimmed.size() > 1;
}

bool IsTableSeparator(const QString &line)
{
  static const QRegularExpression separator(lit("^\\s*\\|?(\\s*:?-{2,}:?\\s*\\|)+\\s*:?-*:?\\s*$"));
  return separator.match(line).hasMatch();
}

QStringList TableCells(const QString &line)
{
  QString trimmed = line.trimmed();
  if(trimmed.startsWith(QLatin1Char('|')))
    trimmed.remove(0, 1);
  if(trimmed.endsWith(QLatin1Char('|')))
    trimmed.chop(1);
  QStringList cells = trimmed.split(QLatin1Char('|'));
  for(QString &cell : cells)
    cell = cell.trimmed();
  return cells;
}
}

QString AgentMarkdown::Escape(const QString &text)
{
  return text.toHtmlEscaped();
}

QString AgentMarkdown::InlineToHtml(const QString &text)
{
  QString html;
  int pos = 0;
  while(pos < text.size())
  {
    int open = text.indexOf(QLatin1Char('`'), pos);
    int close = open >= 0 ? text.indexOf(QLatin1Char('`'), open + 1) : -1;
    if(open < 0 || close < 0)
    {
      html += BoldAndLinks(Escape(text.mid(pos)));
      break;
    }
    html += BoldAndLinks(Escape(text.mid(pos, open - pos)));
    html += lit("<code style=\"%1 background-color: rgba(127,127,127,40);\">%2</code>")
                .arg(QString::fromLatin1(CodeStyle), Escape(text.mid(open + 1, close - open - 1)));
    pos = close + 1;
  }
  return html;
}

QString AgentMarkdown::ToHtml(const QString &markdown)
{
  static const QRegularExpression heading(lit("^(#{1,6})\\s+(.*)$"));
  static const QRegularExpression bullet(lit("^\\s*[-*+]\\s+(.*)$"));
  static const QRegularExpression numbered(lit("^\\s*\\d+[.)]\\s+(.*)$"));

  QStringList lines = markdown.split(QLatin1Char('\n'));
  QString html;
  QStringList paragraph;
  QString listTag;

  auto flushParagraph = [&]() {
    if(paragraph.isEmpty())
      return;
    QStringList parts;
    for(const QString &line : paragraph)
      parts << InlineToHtml(line);
    html += lit("<p>") + parts.join(lit("<br/>")) + lit("</p>");
    paragraph.clear();
  };
  auto closeList = [&]() {
    if(listTag.isEmpty())
      return;
    html += lit("</%1>").arg(listTag);
    listTag.clear();
  };

  for(int i = 0; i < lines.size(); i++)
  {
    QString line = lines[i];
    if(line.endsWith(QLatin1Char('\r')))
      line.chop(1);
    QString trimmed = line.trimmed();

    if(trimmed.startsWith(lit("```")))
    {
      flushParagraph();
      closeList();
      QStringList code;
      for(i++; i < lines.size(); i++)
      {
        QString codeLine = lines[i];
        if(codeLine.endsWith(QLatin1Char('\r')))
          codeLine.chop(1);
        if(codeLine.trimmed().startsWith(lit("```")))
          break;
        code << codeLine;
      }
      html += lit("<pre style=\"%1 background-color: rgba(127,127,127,30);\">%2</pre>")
                  .arg(QString::fromLatin1(CodeStyle), Escape(code.join(lit("\n"))));
      continue;
    }

    if(trimmed.isEmpty())
    {
      flushParagraph();
      closeList();
      continue;
    }

    if(IsTableRow(line) && i + 1 < lines.size() && IsTableSeparator(lines[i + 1]))
    {
      flushParagraph();
      closeList();
      html += lit("<table border=\"1\" cellspacing=\"0\" cellpadding=\"3\"><tr>");
      for(const QString &cell : TableCells(line))
        html += lit("<th>%1</th>").arg(InlineToHtml(cell));
      html += lit("</tr>");
      for(i += 2; i < lines.size() && IsTableRow(lines[i]); i++)
      {
        html += lit("<tr>");
        for(const QString &cell : TableCells(lines[i]))
          html += lit("<td>%1</td>").arg(InlineToHtml(cell));
        html += lit("</tr>");
      }
      i--;
      html += lit("</table>");
      continue;
    }

    QRegularExpressionMatch match = heading.match(trimmed);
    if(match.hasMatch())
    {
      flushParagraph();
      closeList();
      int level = qMin(6, match.captured(1).size() + 2);
      html += lit("<h%1>%2</h%1>").arg(level).arg(InlineToHtml(match.captured(2)));
      continue;
    }

    match = bullet.match(line);
    QString tag = lit("ul");
    if(!match.hasMatch())
    {
      match = numbered.match(line);
      tag = lit("ol");
    }
    if(match.hasMatch())
    {
      flushParagraph();
      if(listTag != tag)
      {
        closeList();
        html += lit("<%1>").arg(tag);
        listTag = tag;
      }
      html += lit("<li>%1</li>").arg(InlineToHtml(match.captured(1)));
      continue;
    }

    if(!listTag.isEmpty() && (line.startsWith(lit("  ")) || line.startsWith(QLatin1Char('\t'))))
    {
      if(html.endsWith(lit("</li>")))
        html.chop(5);
      html += lit("<br/>") + InlineToHtml(trimmed) + lit("</li>");
      continue;
    }

    closeList();
    paragraph << line;
  }

  flushParagraph();
  closeList();
  return html;
}
