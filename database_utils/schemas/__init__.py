from .client import *
from .company import *
from .custom_field import *
from .invoice import *
from .notification import *
from .order import *
from .order_item import *
from .payment import *
from .permission import *
from .product import *
from .recurring_order import *
from .requests import *
from .role import *
from .task import *
from .task_state import *
from .task_template import *
from .user import *
from .workflow import *
from .integration import *
from .service_plan import *
from .client_service import *
from .inventory import *
# schemas/network.py deleted (Cycle 2 D6, revision c2d_graph_removal — the
# network graph is gone); .topology (D5) is its replacement.
from .playbook import *
from .workflow_template import *
from .insight import *
# Cycle 5 Phase 1: network configuration (TR-069 / GenieACS)
from .device_credential import *
from .network_access import *
from .acs_registration import *
from .provisioning_settings import *
from .device_action_log import *
# uplink-mobile integration (rs1/tc1/uf1/pi1)
from .mobile_integration import *

# Rebuild models with forward references to resolve circular dependencies
from .order import OrderOut
from .recurring_order import (
    RecurringOrderOut,
    RecurringOrderCreateResponse,
    OrderGenerationResponse,
    GeneratedOrdersWithGaps,
    RegeneratePeriodResponse,
)

OrderOut.model_rebuild()
RecurringOrderOut.model_rebuild()
RecurringOrderCreateResponse.model_rebuild()
OrderGenerationResponse.model_rebuild()
GeneratedOrdersWithGaps.model_rebuild()
RegeneratePeriodResponse.model_rebuild()