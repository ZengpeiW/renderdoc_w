TEMPLATE = app
TARGET = agent_history_test
QT += core gui widgets
CONFIG += console c++11
CONFIG -= app_bundle
INCLUDEPATH += ../../../qrenderdoc/Code/Agent
SOURCES += agent_history_test.cpp \
           ../../../qrenderdoc/Code/Agent/AgentHistory.cpp
HEADERS += ../../../qrenderdoc/Code/Agent/AgentHistory.h
