import ReactMarkdown, { type Components } from "react-markdown"
import remarkGfm from "remark-gfm"

// Loaded lazily through <Md> (react-markdown + unified is ~120 kB). remark-gfm adds the tables,
// task lists and strikethrough the agents' reports use.
// Raw HTML in model output is dropped (skipHtml); react-markdown's default urlTransform strips
// javascript: and other unsafe link targets.
const components: Components = {
  a: ({ href, title, children }) => (
    <a href={href} title={title} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  ),
  // Reports sit under the run's h2/h3 headings: demote model headings so the outline stays valid.
  h1: ({ children }) => <h1 aria-level={4}>{children}</h1>,
  h2: ({ children }) => <h2 aria-level={5}>{children}</h2>,
  h3: ({ children }) => <h3 aria-level={6}>{children}</h3>,
  h4: ({ children }) => <h4 aria-level={6}>{children}</h4>,
  table: ({ children }) => (
    <div className="my-3 max-w-full overflow-x-auto">
      <table>{children}</table>
    </div>
  ),
}

const plugins = [remarkGfm]

export default function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown skipHtml remarkPlugins={plugins} components={components}>
      {children}
    </ReactMarkdown>
  )
}
