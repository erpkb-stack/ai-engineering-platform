"""identity schema - owner: api. Users, RBAC and groups (groups drive document access)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aeoi_db.base import Base, created_at_col, uuid_pk

SCHEMA = "identity"
ROLES = ("ENGINEER", "SRE", "INCIDENT_COMMANDER", "MANAGER", "ADMIN")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        # serves: login lookup by email (case-normalised)
        Index("ix_users_email", "email", unique=True),
        {"schema": SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    # OIDC "sub" claim: the stable identity from the IdP (email can change).
    external_subject: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = created_at_col()


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = {"schema": SCHEMA}
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class Permission(Base):
    __tablename__ = "permissions"
    __table_args__ = (
        CheckConstraint("name ~ '^[a-z_]+:[a-z_]+(:[a-z_]+)?$'", name="name_format"),
        {"schema": SCHEMA},
    )
    name: Mapped[str] = mapped_column(String(80), primary_key=True)  # e.g. logs:read
    description: Mapped[str] = mapped_column(Text, nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        # serves: "which roles grant permission X" (PK covers role -> permissions)
        Index("ix_role_permissions_permission_name", "permission_name"),
        {"schema": SCHEMA},
    )
    role_name: Mapped[str] = mapped_column(
        ForeignKey(f"{SCHEMA}.roles.name", ondelete="CASCADE"), primary_key=True
    )
    permission_name: Mapped[str] = mapped_column(
        ForeignKey(f"{SCHEMA}.permissions.name", ondelete="CASCADE"), primary_key=True
    )


class UserRole(Base):
    __tablename__ = "user_roles"
    __table_args__ = (
        # serves: "who has role X" (e.g. list incident commanders); PK covers user -> roles
        Index("ix_user_roles_role_name", "role_name"),
        {"schema": SCHEMA},
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), primary_key=True
    )
    role_name: Mapped[str] = mapped_column(
        ForeignKey(f"{SCHEMA}.roles.name", ondelete="RESTRICT"), primary_key=True
    )
    granted_at: Mapped[datetime] = created_at_col()
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Group(Base):
    """Access groups (e.g. security-team). Documents list the groups allowed to read them."""

    __tablename__ = "groups"
    __table_args__ = (
        CheckConstraint("name ~ '^[a-z0-9-]+$'", name="name_format"),
        {"schema": SCHEMA},
    )
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)


class UserGroup(Base):
    __tablename__ = "user_groups"
    __table_args__ = (
        # serves: "members of group X" (ACL audits); PK covers user -> groups at login
        Index("ix_user_groups_group_name", "group_name"),
        {"schema": SCHEMA},
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"), primary_key=True
    )
    group_name: Mapped[str] = mapped_column(
        ForeignKey(f"{SCHEMA}.groups.name", ondelete="RESTRICT"), primary_key=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
