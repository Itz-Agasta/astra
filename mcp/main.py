from fastmcp import FastMCP
from tools.point_to import register as register_point_to
from tools.manual_slew import register as register_manual_slew
from tools.list_visible_objects import register as register_list_visible_objects

def create_server() -> FastMCP:
    mcp = FastMCP("astra-telescope")
    register_point_to(mcp)
    register_manual_slew(mcp)
    register_list_visible_objects(mcp)
    return mcp


def main():
    server = create_server()
    server.run()


if __name__ == "__main__":
    main()