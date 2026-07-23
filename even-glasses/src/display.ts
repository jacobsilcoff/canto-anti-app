export function safeContainerContent(content: string): string {
  return content.length === 0 ? ' ' : content
}
