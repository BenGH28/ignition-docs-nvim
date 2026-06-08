# ignition-docs.nvim

Ignition 8.3 scripting function documentation as vim help files.

Browse and search all `system.*` scripting functions directly inside Neovim with `:help`.

## Usage

```vim
:help ignition              " main index
:help ignition-modules      " browse all modules
:help ignition-system.tag   " jump to a module
:help ignition-system.tag.readBlocking  " jump to a specific function
```

## Installation

### lazy.nvim

```lua
{
  "BenGH28/ignition-docs-nvim",
  lazy = false,  -- required so help files are available immediately
}
```

### vim-plug

```vim
Plug 'BenGH28/ignition-docs-nvim'
```

### Manual

Clone the repo anywhere on your `runtimepath` and run `:helptags ALL`.

## Modules

| Module | Description |
|--------|-------------|
| `system.alarm` | Alarm acknowledgement, shelving, querying |
| `system.bacnet` | BACnet protocol operations |
| `system.dataset` | Dataset creation and manipulation |
| `system.date` | Date/time formatting and arithmetic |
| `system.db` | Database queries and transactions |
| `system.device` | Device configuration |
| `system.dnp` / `system.dnp3` | DNP protocol commands |
| `system.eam` | Enterprise asset management |
| `system.eventstream` | Event publishing |
| `system.file` | File I/O |
| `system.groups` | Group management |
| `system.historian` | Historical data queries |
| `system.iec61850` | IEC 61850 protocol |
| `system.kafka` | Kafka messaging |
| `system.math` | Statistical calculations |
| `system.mongodb` | MongoDB operations |
| `system.net` | Network utilities |
| `system.opc` / `system.opchda` / `system.opcua` | OPC protocols |
| `system.perspective` | Perspective session management |
| `system.print` | Printer operations |
| `system.project` | Project utilities |
| `system.report` | Report execution |
| `system.roster` | User roster management |
| `system.secrets` | Credential management |
| `system.secsgem` | SECS/GEM protocol |
| `system.security` | User authentication |
| `system.serial` | Serial port communication |
| `system.sfc` | Sequential function charts |
| `system.tag` | Tag read/write operations |
| `system.twilio` | SMS/messaging integration |
| `system.user` | User and role administration |
| `system.util` | General utilities |
| `system.vision` | Vision client operations |

## Updating the docs

The help files are generated from the [Inductive Automation docs site](https://docs.inductiveautomation.com/docs/8.3/appendix/scripting-functions) by a scraper script.

```sh
pip install requests beautifulsoup4
python scripts/scrape.py
nvim --headless -c "helptags doc/" -c "quit"
```
