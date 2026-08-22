from fastmcp import FastMCP
from tools.point_to import register as register_point_to
from tools.manual_slew import register as register_manual_slew
from tools.list_visible_objects import register as register_list_visible_objects
from tools.get_telescope_status import register as register_get_telescope_status
from tools.get_current_orientation import register as register_get_current_orientation
from tools.get_calibration_status import register as register_get_calibration_status
from tools.abort_calibration import register as register_abort_calibration

def create_server() -> FastMCP:
    mcp = FastMCP("astra-telescope")
    register_point_to(mcp)
    register_manual_slew(mcp)
    register_list_visible_objects(mcp)
    register_get_telescope_status(mcp)
    register_get_current_orientation(mcp)
    register_get_calibration_status(mcp)
    register_abort_calibration(mcp)
    return mcp


def main():
    server = create_server()
    server.run()


if __name__ == "__main__":
    main()