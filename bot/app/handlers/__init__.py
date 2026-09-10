"""Router assembly. Order matters.

`quick` ends with a catch-all for any plain text, so it has to be last: anything
registered after it would never see a message. Within `quick`, the commands and
FSM states are declared before that catch-all for the same reason.
"""

from aiogram import Router

from app.handlers.cards import router as cards_router
from app.handlers.common import router as common_router
from app.handlers.lists import router as lists_router
from app.handlers.new import router as new_router
from app.handlers.quick import router as quick_router

router = Router(name="root")
router.include_router(common_router)
router.include_router(lists_router)
router.include_router(new_router)
router.include_router(cards_router)
router.include_router(quick_router)

__all__ = ["router"]
