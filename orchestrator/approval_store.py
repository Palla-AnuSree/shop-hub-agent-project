
import os
import json
from psycopg import AsyncConnection


DATABASE_URL = os.getenv("DATABASE_URL")


async def initialize_approval_store():
    """Create the approval proposals table if it does not exist."""
    async with await AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS approval_proposals (
                proposal_id TEXT PRIMARY KEY,
                merchant TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                proposal JSONB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                resolved_at TIMESTAMPTZ
            )
        """)
        await conn.commit()


async def save_proposal(
    proposal_id: str,
    merchant: str,
    thread_id: str,
    proposal: dict
):
    async with await AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(
            """
            INSERT INTO approval_proposals
                (proposal_id, merchant, thread_id, proposal)
            VALUES (%s, %s, %s, %s)
            """,
            (proposal_id, merchant, thread_id, json.dumps(proposal))
        )
        await conn.commit()


async def get_proposal(proposal_id: str, merchant: str):
    async with await AsyncConnection.connect(DATABASE_URL) as conn:
        cursor = await conn.execute(
            """
            SELECT proposal_id, merchant, thread_id, proposal, status
            FROM approval_proposals
            WHERE proposal_id = %s AND merchant = %s
            """,
            (proposal_id, merchant)
        )
        row = await cursor.fetchone()

    if not row:
        return None

    return {
        "proposal_id": row[0],
        "merchant": row[1],
        "thread_id": row[2],
        "proposal": row[3],
        "status": row[4]
    }



async def resolve_proposal(
    proposal_id: str,
    merchant: str,
    decision: str,
    expected_status: str = "pending"
):
    """Update a proposal only when its current status matches."""
    async with await AsyncConnection.connect(DATABASE_URL) as conn:
        cursor = await conn.execute(
            """
            UPDATE approval_proposals
            SET status = %s,
                resolved_at = CASE
                    WHEN %s IN ('approved', 'rejected')
                    THEN NOW()
                    ELSE resolved_at
                END
            WHERE proposal_id = %s
              AND merchant = %s
              AND status = %s
            RETURNING proposal_id
            """,
            (
                decision,
                decision,
                proposal_id,
                merchant,
                expected_status
            )
        )
        row = await cursor.fetchone()
        await conn.commit()

    return row is not None
