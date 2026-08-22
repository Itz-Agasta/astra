from fastmcp import FastMCP
from tools.point_to import register as register_point_to


def create_server() -> FastMCP:
    mcp = FastMCP("astra-telescope")
    register_point_to(mcp)
    return mcp


def main():
    server = create_server()
    server.run()


if __name__ == "__main__":
    main()