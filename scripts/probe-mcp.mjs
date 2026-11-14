/**
 * 用与 DeepSeek Harness 0.2.2 相同的 @modelcontextprotocol/client@2.2.0
 * 连接本仓库的 stdio MCP，并打印工具名。密钥不会传给子进程。
 *
 * 用法：在已安装该客户端的目录执行
 *   RAIL_STUDIO_ROOT=/path/to/rail-content-studio node scripts/probe-mcp.mjs
 */
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { Client } from '@modelcontextprotocol/client';
import { StdioClientTransport } from '@modelcontextprotocol/client/stdio';

const studio = process.env.RAIL_STUDIO_ROOT || '';
const cwd = process.env.RAIL_PROJECT_ROOT
  || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const env = {};
for (const [key, value] of Object.entries(process.env)) {
  if (typeof value === 'string' && !/KEY|TOKEN|SECRET|PASSWORD/i.test(key)) env[key] = value;
}
env.RAIL_STUDIO_ROOT = studio;
env.RAIL_OUTPUT_DIR = process.env.RAIL_OUTPUT_DIR || path.join(cwd, '.run', 'probe-output');

const transport = new StdioClientTransport({
  command: process.env.RAIL_PYTHON || 'python3',
  args: ['-m', 'mcp_server'],
  cwd,
  env,
  stderr: 'pipe',
});

const client = new Client({ name: 'rail-studio-probe', version: '0.1.0' });
const mode = process.env.PROBE_MODE || 'legacy';
client.setVersionNegotiation({ mode });
transport.stderr?.on('data', (chunk) => process.stderr.write(chunk));
await client.connect(transport);
const listed = await client.listTools();
const names = listed.tools.map((tool) => tool.name).sort();
console.log(names.join('\n'));
await client.close();
