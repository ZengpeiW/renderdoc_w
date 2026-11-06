TEMPLATE = app
TARGET = agent_conversation_identity_test
QT = core
CONFIG += console c++11
CONFIG -= app_bundle
# Production builds provide lit() through precompiled.h; this standalone target has no GUI PCH.
DEFINES += lit=QStringLiteral QT_NO_CAST_FROM_ASCII QT_NO_CAST_TO_ASCII
INCLUDEPATH += ../../../qrenderdoc/Code/Agent
SOURCES += agent_conversation_identity_test.cpp \
           ../../../qrenderdoc/Code/Agent/AgentConversation.cpp \
           ../../../qrenderdoc/Code/Agent/AgentCaptureIdentity.cpp
HEADERS += ../../../qrenderdoc/Code/Agent/AgentConversation.h \
           ../../../qrenderdoc/Code/Agent/AgentCaptureIdentity.h
